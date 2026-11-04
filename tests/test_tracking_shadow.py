import unittest
from unittest.mock import patch
import test_v1 as base
from models import db, Shipment


class ShadowTests(unittest.TestCase):
    setUpClass = classmethod(base.StabilityTests.setUpClass.__func__)
    tearDown = base.StabilityTests.tearDown
    token = base.StabilityTests.token
    post = base.StabilityTests.post
    login = base.StabilityTests.login
    def setUp(self):
        base.StabilityTests.setUp(self)
        self.addCleanup(self.app.config.pop, 'TRACKING_MODE', None)
        self.shipment.tracking_number = 'DEMO-001'
        self.agent.supports_api = True
        db.session.commit()
        from isolated_runtime import register_shadow_fixture
        register_shadow_fixture(self.app, self.shipment)
        self.login()
    def query(self, **data): return self.post('/track', data={'numbers':'DEMO-001', **data})
    def test_shadow_one_response(self):
        self.app.config['TRACKING_MODE'] = 'shadow'
        import comparison.runner as runner
        original = runner.run_legacy
        captured = []
        def old(*args): captured.append(args[3]); return original(*args)
        import tracking_integration as integration
        original_query = integration.query_tracking
        def new(plan, provider):
            r = provider.fetch(plan.attempts[0])
            self.assertIs(r.body, captured[0])
            return original_query(plan, provider)
        with patch('tracking_integration.Response', wraps=integration.Response) as acquisition, patch('comparison.runner.run_legacy', side_effect=old) as old_call, patch('tracking_integration.query_tracking', side_effect=new), patch('requests.post', side_effect=AssertionError('network')):
            r = self.query()
        self.assertEqual(r.status_code, 200)
        self.assertIn('虚构模拟轨迹', r.get_data(as_text=True))
        self.assertEqual(old_call.call_count, 1)
        self.assertEqual(acquisition.call_count, 1)
    def test_comparison_failure_returns_old(self):
        self.app.config['TRACKING_MODE']='shadow'
        with patch('tracking_integration.query_tracking', side_effect=RuntimeError('secret')), patch('comparison.runner.run_legacy', wraps=__import__('comparison.runner', fromlist=['run_legacy']).run_legacy) as old:
            r=self.query()
        self.assertIn('虚构模拟轨迹',r.get_data(as_text=True)); self.assertEqual(old.call_count,1)
    def test_service_disabled(self):
        self.app.config['TRACKING_MODE']='service'
        with patch('views.fetch_tracking_from_api', side_effect=AssertionError('no calls')): self.assertIn('禁止',self.query().get_data(as_text=True))
    def test_nonisolated_shadow_disabled(self):
        self.app.config['TRACKING_MODE']='shadow'
        with patch.dict(self.app.config, ISOLATED_MODE=False), patch('views.fetch_tracking_from_api',side_effect=AssertionError('no calls')):
            self.assertIn('禁止',self.query().get_data(as_text=True))
    def test_default_and_unknown_legacy(self):
        for mode in ('legacy','garbage'):
            self.app.config['TRACKING_MODE']=mode
            with patch('views.fetch_tracking_from_api',return_value=([],None)) as f:
                self.query(); self.assertEqual(f.call_count,1)
    def test_request_cannot_set_mode(self):
        with patch('views.fetch_tracking_from_api',return_value=([],None)) as f:
            self.query(TRACKING_MODE='shadow',verified='true'); self.assertEqual(f.call_count,1)
    def test_duplicate_identity(self):
        self.app.config['TRACKING_MODE']='shadow'
        db.session.add(Shipment(tracking_number='DEMO-001',agent_id=self.agent.id));db.session.commit()
        self.assertIn('不唯一',self.query().get_data(as_text=True))
    def test_agent_filter_conflict(self):
        self.app.config['TRACKING_MODE']='shadow'
        self.assertIn('不一致',self.query(agent_id='999').get_data(as_text=True))
    def test_customer_filter_conflict(self):
        self.app.config['TRACKING_MODE']='shadow'
        self.assertIn('不一致',self.query(customer_id='999').get_data(as_text=True))
    def test_inactive(self):
        self.app.config['TRACKING_MODE']='shadow';self.agent.is_active=False;db.session.commit()
        self.assertNotIn('模拟对比完成',self.query().get_data(as_text=True))
    def test_csrf(self):
        self.app.config['TRACKING_MODE']='shadow'
        self.assertEqual(self.client.post('/track',data={'numbers':'DEMO-001'}).status_code,400)
    def test_anonymous(self):
        self.post('/logout');self.assertEqual(self.client.get('/track').status_code,302)
    def test_html_escaped(self):
        self.app.config['TRACKING_MODE']='shadow'
        from datetime import datetime
        from models import ManualTrack
        db.session.add(ManualTrack(shipment_id=self.shipment.id,happen_time=datetime(2026,1,1),description='<script>fake</script>'));db.session.commit()
        text=self.query().get_data(as_text=True)
        self.assertIn('&lt;script&gt;fake&lt;/script&gt;',text);self.assertNotIn('<script>fake</script>',text)
