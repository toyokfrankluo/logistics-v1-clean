"""Legacy /track identity regressions using only disposable fictional records."""
import unittest
from unittest.mock import patch, Mock
from datetime import datetime
import test_v1 as baseline
from models import db, Shipment, Customer, CarrierAgent, ManualTrack
import views


class IdentityTests(unittest.TestCase):
    setUpClass = classmethod(baseline.StabilityTests.setUpClass.__func__)
    tearDown = baseline.StabilityTests.tearDown
    token = baseline.StabilityTests.token
    post = baseline.StabilityTests.post
    login = baseline.StabilityTests.login
    def setUp(self):
        baseline.StabilityTests.setUp(self)
        self.login()
        self.customer2 = Customer(name='虚构客户乙')
        self.agent2 = CarrierAgent(name='虚构代理乙', supports_api=True, api_url='https://fictional.nextsls.com/api')
        db.session.add_all([self.customer2,self.agent2]);db.session.commit()
    def duplicate(self, customer=None, agent=None):
        row=Shipment(tracking_number=self.shipment.tracking_number, shipment_id='OTHER-PROVIDER', customer_id=(customer or self.customer).id,agent_id=(agent or self.agent).id)
        db.session.add(row);db.session.commit();return row
    def query(self, **filters):
        return self.post('/track',data={'numbers':self.shipment.tracking_number,**filters})
    def test_customer_conflict_no_calls(self):
        with patch('views.fetch_tracking_from_api') as f:
            r=self.query(customer_id=str(self.customer2.id))
        self.assertEqual(f.call_count,0);self.assertIn('当前查询范围内未找到运单',r.get_data(as_text=True))
    def test_agent_conflict_no_calls(self):
        with patch('views.fetch_tracking_from_api') as f:self.query(agent_id=str(self.agent2.id))
        self.assertEqual(f.call_count,0)
    def test_cross_customer_selects_filtered_record(self):
        row=self.duplicate(customer=self.customer2)
        with patch('views.fetch_tracking_from_api',return_value=([],None)) as f:self.query(customer_id=str(self.customer2.id))
        self.assertIs(f.call_args.kwargs['selected_shipment'],row)
    def test_cross_agent_selects_filtered_record(self):
        row=self.duplicate(agent=self.agent2)
        with patch('views.fetch_tracking_from_api',return_value=([],None)) as f:self.query(agent_id=str(self.agent2.id))
        self.assertIs(f.call_args.kwargs['selected_shipment'],row)
    def test_ambiguity_no_calls(self):
        self.duplicate()
        with patch('views.fetch_tracking_from_api') as f:r=self.query(customer_id=str(self.customer.id))
        self.assertEqual(f.call_count,0);self.assertIn('不唯一',r.get_data(as_text=True))
    def test_unfiltered_cross_customer_ambiguous(self):
        self.duplicate(customer=self.customer2)
        with patch('views.fetch_tracking_from_api') as f:self.query()
        self.assertEqual(f.call_count,0)
    def test_batch_keeps_ids_and_same_number_results(self):
        row=self.duplicate(customer=self.customer2,agent=self.agent2)
        for s,text in ((self.shipment,'虚构历史甲'),(row,'虚构历史乙')):
            db.session.add(ManualTrack(shipment_id=s.id,happen_time=datetime(2026,1,1),description=text))
        db.session.commit()
        with patch('views.fetch_tracking_from_api',return_value=([],None)) as f:r=self.query(numbers='')
        ids=[c.kwargs['selected_shipment'].id for c in f.call_args_list]
        self.assertEqual(set(ids),{self.shipment.id,row.id});self.assertEqual(len(ids),2)
        text=r.get_data(as_text=True);self.assertIn('虚构历史甲',text);self.assertIn('虚构历史乙',text)
    def test_batch_filter_retains_row(self):
        row=self.duplicate(customer=self.customer2)
        with patch('views.fetch_tracking_from_api',return_value=([],None)) as f:self.query(numbers='',customer_id=str(self.customer2.id))
        self.assertEqual(f.call_count,1);self.assertIs(f.call_args.kwargs['selected_shipment'],row)
    def test_nextsls_selected_no_requery(self):
        row=self.duplicate(agent=self.agent2)
        response=Mock(status_code=200);response.json.return_value={'status':1,'data':{'shipment':{'traces':[]}}}
        with patch('views.requests.post',return_value=response) as remote, patch.object(Shipment,'query') as q:
            tracks,error=views.fetch_tracking_from_api(self.agent2,row.tracking_number,selected_shipment=row)
            q.filter_by.assert_not_called()
        self.assertEqual((tracks,error),([],None))
        self.assertEqual(remote.call_args.kwargs['json']['shipment']['shipment_id'],'OTHER-PROVIDER')
    def test_missing_nextsls_provider_fallback_unchanged(self):
        row=self.duplicate(agent=self.agent2);row.shipment_id=None;db.session.commit()
        response=Mock(status_code=200);response.json.return_value={'status':1,'data':{'shipment':{'traces':[]}}}
        with patch('views.requests.post',return_value=response) as remote:
            views.fetch_tracking_from_api(self.agent2,row.tracking_number,selected_shipment=row)
        self.assertEqual(remote.call_args.kwargs['json']['shipment']['shipment_id'],row.tracking_number)
    def test_helper_mismatched_agent_no_calls(self):
        with patch('views.requests.post') as remote:
            _,error=views.fetch_tracking_from_api(self.agent2,self.shipment.tracking_number,selected_shipment=self.shipment)
        self.assertIsNotNone(error);remote.assert_not_called()
    def test_unique_text_once(self):
        event={'time':'T','location':'L','description':'虚构完整事件'}
        with patch('views.fetch_tracking_from_api',return_value=([event],None)) as f:r=self.query()
        self.assertEqual(f.call_count,1);self.assertIs(f.call_args.kwargs['selected_shipment'],self.shipment)
        self.assertIn('T L 虚构完整事件',r.get_data(as_text=True))
    def test_repeated_input_one_record(self):
        with patch('views.fetch_tracking_from_api',return_value=([],None)) as f:self.query(numbers=self.shipment.tracking_number+'\n'+self.shipment.tracking_number)
        self.assertEqual(f.call_count,1)
