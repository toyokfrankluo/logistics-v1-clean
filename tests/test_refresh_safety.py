import unittest
from unittest.mock import patch, Mock
from datetime import datetime
import contextlib
import io
import test_v1 as base
import views
from models import db, Shipment, Customer, CarrierAgent


class RefreshSafety(unittest.TestCase):
    setUpClass=classmethod(base.StabilityTests.setUpClass.__func__)
    tearDown=base.StabilityTests.tearDown
    token=base.StabilityTests.token
    post=base.StabilityTests.post
    login=base.StabilityTests.login
    def setUp(self):
        base.StabilityTests.setUp(self)
        self.agent.supports_api=True;self.agent.api_url='https://fictional.nextsls.com/SECRET'
        self.shipment.created_at=datetime.utcnow();db.session.commit();self.login()
    def duplicate(self, other_agent=False):
        c=Customer(name='虚构乙'+str(other_agent));a=CarrierAgent(name='虚构乙代理'+str(other_agent),supports_api=True,api_url='https://fictional.nextsls.com/api')
        db.session.add_all([c,a]);db.session.flush()
        s=Shipment(tracking_number=self.shipment.tracking_number,shipment_id='OTHER',customer_id=c.id,agent_id=a.id if other_agent else self.agent.id)
        db.session.add(s);db.session.commit();return s
    def test_same_number_blocks_both_cloud_targets(self):
        for other in (False,True):
            row=self.duplicate(other)
            with patch('views.fetch_tracking_from_api') as fetch,patch('views.requests.post') as remote:
                for s in (self.shipment,row):
                    tracks,error=views._refresh_query(s,s.agent)
                    self.assertIsNone(tracks);self.assertEqual(error,'refresh:ambiguous_identity')
                    self.assertEqual(views.simple_sync_tracking(s,[]),0)
                    self.assertEqual(views.sync_tracking_to_supabase(s,[]),(0,1))
            fetch.assert_not_called();remote.assert_not_called()
            db.session.delete(row);db.session.commit()
    def test_unique_nextsls_keeps_selected_number(self):
        r=Mock(status_code=200);r.json.return_value={'status':1,'data':{'shipment':{'traces':[]}}}
        with patch('views.requests.post',return_value=r) as remote:
            tracks,error=views._refresh_query(self.shipment,self.agent)
        self.assertEqual((tracks,error),([],None))
        self.assertEqual(remote.call_count,1)
        self.assertEqual(remote.call_args.kwargs['json']['shipment']['shipment_id'],self.shipment.shipment_id)
    def test_disabled_all_routes_zero_query(self):
        self.agent.is_active=False;db.session.commit()
        paths=['/admin/sync-to-supabase','/admin/sync-tracking-details','/admin/refresh-tracking',f'/shipments/{self.shipment.id}/refresh',f'/admin/refresh-full-history/{self.shipment.id}',f'/admin/auto-sync-single/{self.shipment.id}','/admin/auto-sync-all-safe','/admin/auto-sync-all','/admin/auto-sync-recent']
        with patch('views.fetch_tracking_from_api') as fetch,patch('views.requests.post') as remote,patch('time.sleep'):
            for path in paths:self.assertEqual(self.post(path).status_code,302)
        fetch.assert_not_called();remote.assert_not_called()
    def test_failure_no_write_and_redaction(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out),patch('views.requests.post',side_effect=RuntimeError('SECRET_TOKEN /private/secret')) as remote,patch('views.simple_sync_tracking') as write:
            self.post(f'/shipments/{self.shipment.id}/refresh')
        write.assert_not_called();self.assertEqual(remote.call_count,1)
        with self.client.session_transaction() as session:text=str(session.get('_flashes'))
        self.assertNotIn('SECRET',text+out.getvalue());self.assertNotIn('/private/',text+out.getvalue())
    def test_batch_keeps_objects_without_number_dictionary(self):
        row=self.duplicate(True)
        with patch('views._refresh_query',return_value=([{'time':'T','description':'虚构'}],None)) as query,patch('views.sync_tracking_to_supabase',return_value=(1,0)) as write:
            self.post('/admin/sync-tracking-details')
        self.assertEqual({c.args[0].id for c in query.call_args_list},{self.shipment.id,row.id})
        self.assertEqual({c.args[0].id for c in write.call_args_list},{self.shipment.id,row.id})
        self.assertEqual(write.call_count,2)
    def test_broken_history_blocks_before_query(self):
        with patch('views.fetch_tracking_from_api') as fetch:
            _,error=views._refresh_query(self.shipment,self.agent,history=True)
            self.assertEqual(error,'refresh:history_mapping_unavailable')
            self.post(f'/admin/refresh-full-history/{self.shipment.id}')
        fetch.assert_not_called()
    def test_unique_success_same_target(self):
        with patch('views.fetch_tracking_from_api',return_value=([{'time':'T','description':'虚构'}],None)) as fetch,patch('views.simple_sync_tracking',return_value=1) as write:
            self.post(f'/shipments/{self.shipment.id}/refresh')
        self.assertEqual(fetch.call_count,1)
        self.assertIs(fetch.call_args.kwargs['selected_shipment'],self.shipment)
        self.assertIs(write.call_args.args[0],self.shipment)
    def test_metadata_duplicate_skipped(self):
        self.duplicate(True)
        with patch.dict('os.environ',SUPABASE_KEY='fake'),patch('views.requests.post') as remote:
            self.post('/admin/sync-to-supabase')
        remote.assert_not_called()
