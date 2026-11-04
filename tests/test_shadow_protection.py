"""Fault injection only against the disposable isolated app."""
import unittest
from unittest.mock import patch
import test_tracking_shadow as original
import tracking_integration as integration
import isolated_runtime
from models import db


class ProtectionTests(unittest.TestCase):
    setUpClass = classmethod(original.ShadowTests.setUpClass.__func__)
    setUp = original.ShadowTests.setUp
    tearDown = original.ShadowTests.tearDown
    token = original.ShadowTests.token
    post = original.ShadowTests.post
    login = original.ShadowTests.login
    query = original.ShadowTests.query
    def fail(self, target, old_valid=False, **kwargs):
        self.app.config['TRACKING_MODE']='shadow'
        with patch(target, **kwargs) as fault, patch('views.fetch_tracking_from_api', side_effect=AssertionError('no retry')):
            with self.assertLogs(self.app.logger, level='INFO') as logs:
                r=self.query()
        text=r.get_data(as_text=True)
        self.assertEqual(r.status_code,200)
        self.assertNotIn('SECRET_PATH_TOKEN',text)
        self.assertNotIn('SECRET_PATH_TOKEN',' '.join(logs.output))
        if old_valid: self.assertIn('虚构模拟轨迹',text)
        else:
            self.assertIn('未生成有效旧结果',text)
            self.assertNotIn('虚构模拟轨迹',text)
        return fault
    def test_provider_failure_once(self):
        f=self.fail('tracking_integration.Response',side_effect=RuntimeError('SECRET_PATH_TOKEN'))
        self.assertEqual(f.call_count,1)
    def test_parser_loading_failure(self):
        self.fail('tracking_integration._load_old_parser',side_effect=RuntimeError('SECRET_PATH_TOKEN'))
    def test_old_parser_raises(self):
        parser=lambda *a: (_ for _ in ()).throw(RuntimeError('SECRET_PATH_TOKEN'))
        self.fail('tracking_integration._load_old_parser',return_value=parser)
    def test_old_parser_bad_structure(self):
        self.fail('tracking_integration._load_old_parser',return_value=lambda *a: {'status':'no_explicit_error','error':None,'exception':None,'events':None})
    def test_old_event_iteration_invalid(self):
        self.fail('tracking_integration._load_old_parser',return_value=lambda *a: {'status':'no_explicit_error','error':None,'exception':None,'events':[{'time': []}]})
    def test_old_failure_not_success(self):
        self.fail('tracking_integration._load_old_parser',return_value=lambda *a: {'status':'error','error':'SECRET_PATH_TOKEN','events':[]})
    def test_new_failure_retains_old(self):
        self.fail('tracking_integration.query_tracking',old_valid=True,side_effect=RuntimeError('SECRET_PATH_TOKEN'))
    def test_comparison_failure_retains_old(self):
        with patch('tracking_integration.Response',wraps=integration.Response) as acquired:
            self.fail('tracking_integration._compare_auto_text',old_valid=True,side_effect=RuntimeError('SECRET_PATH_TOKEN'))
        self.assertEqual(acquired.call_count,1)
    def test_forged_isolation_flag(self):
        self.app.config['TRACKING_MODE']='shadow'
        runtime=isolated_runtime._runtimes.pop(self.app)
        try:
            with patch('tracking_integration.Response') as provider:
                text=self.query(ISOLATED_MODE='true').get_data(as_text=True)
            self.assertIn('禁止',text);provider.assert_not_called()
        finally: isolated_runtime._runtimes[self.app]=runtime
    def test_unregistered_demo(self):
        self.app.config['TRACKING_MODE']='shadow'
        runtime=isolated_runtime._runtimes[self.app]
        entry=runtime['fixtures'].pop(self.shipment.id)
        try:
            with patch('tracking_integration.Response') as provider:
                self.query()
            provider.assert_not_called()
        finally: runtime['fixtures'][self.shipment.id]=entry
    def test_database_config_changed_rejected_before_query(self):
        self.app.config['TRACKING_MODE']='shadow'
        with patch.dict(self.app.config,SQLALCHEMY_DATABASE_URI='sqlite:////DO-NOT-OPEN.db'), patch('tracking_integration.Response') as provider:
            self.assertIn('禁止',self.query().get_data(as_text=True))
        provider.assert_not_called()
    def test_guard_removed_rejected(self):
        self.app.config['TRACKING_MODE']='shadow'
        with patch('socket.create_connection'),patch('tracking_integration.Response') as provider:
            self.assertIn('禁止',self.query().get_data(as_text=True))
        provider.assert_not_called()
    def test_member_no_bypass(self):
        self.post('/logout');self.login(admin=False);self.app.config['TRACKING_MODE']='shadow'
        with patch('tracking_integration.Response') as provider:
            self.assertIn('仅供管理员',self.query().get_data(as_text=True))
        provider.assert_not_called()
    def test_diagnostic_scope(self):
        self.app.config['TRACKING_MODE']='shadow'
        with self.assertLogs(self.app.logger,level='INFO') as logs:self.query()
        self.assertTrue(any('tracking_shadow_auto_text:auto_text_equal' in x for x in logs.output))
        self.assertNotIn('DEMO-001',' '.join(logs.output))

