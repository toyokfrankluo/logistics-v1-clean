"""User-transcribed gettrack specification; synthetic identities only."""
import json
import unittest
from unittest.mock import patch
from tracking import parse_response, QueryStatus, ErrorType
import test_v1 as base
from models import db, ManualTrack

class OfficialParsing(unittest.TestCase):
    def payload(self, events=None):
        return {'success': 1, 'data': [{'server_hawbcode': 'SERVICE-DEMO', 'track_status': 'NT',
                'track_status_name': '转运中', 'details': events if events is not None else [
                    {'track_occur_date': '2026-01-02 10:20:30', 'track_location': '虚构节点',
                     'track_description': '虚构事件'}]}]}
    def parse(self, payload):
        return parse_response('rtb56', 'fictional-agent', 200, json.dumps(payload), expected_number='SERVICE-DEMO')
    def test_success_preserves_fields_order_raw_time(self):
        p=self.payload();p['data'][0]['details'] *= 2
        r=self.parse(p)
        self.assertEqual(r.status, QueryStatus.SUCCESS)
        self.assertEqual(len(r.events),2)
        self.assertEqual(r.events[0].time.raw,'2026-01-02 10:20:30')
        self.assertIsNone(r.events[0].time.occurred_at)
        self.assertEqual(r.events[0].description,'虚构事件')
        self.assertEqual(r.events[0].location,'虚构节点')
        self.assertEqual(r.events[0].status.value,'unknown')
        self.assertEqual(r.shipment_status,'in_transit')
        self.assertEqual(r.service_number,'SERVICE-DEMO')
    def test_strict_success_types(self):
        for v in [0,'0','1',True,1.0]:
            p=self.payload();p['success']=v
            self.assertEqual(self.parse(p).status,QueryStatus.ERROR)
    def test_empty_data_is_unverified(self):
        self.assertEqual(self.parse({'success':1,'data':[]}).status,QueryStatus.UNVERIFIED)
    def test_empty_details(self):
        self.assertEqual(self.parse(self.payload([])).status,QueryStatus.EMPTY)
    def test_identity_mismatch_missing_and_multiple(self):
        for code in [None,'OTHER']:
            p=self.payload();p['data'][0]['server_hawbcode']=code
            self.assertEqual(self.parse(p).status,QueryStatus.ERROR)
        p=self.payload();p['data']*=2
        self.assertEqual(self.parse(p).status,QueryStatus.ERROR)
    def test_english_and_missing_time(self):
        r=self.parse(self.payload([{'track_description':'','track_description_en':'Fictional full event'}]))
        self.assertEqual(r.events[0].description,'Fictional full event')
        self.assertIsNone(r.events[0].time.raw)
    def test_invalid_json(self):
        self.assertEqual(parse_response('rtb56','fake',200,'invalid',expected_number='SERVICE-DEMO').error,ErrorType.INVALID_JSON)

class OfficialBoundary(unittest.TestCase):
    setUpClass=classmethod(base.StabilityTests.setUpClass.__func__)
    setUp=base.StabilityTests.setUp
    tearDown=base.StabilityTests.tearDown
    login=base.StabilityTests.login
    post=base.StabilityTests.post
    token=base.StabilityTests.token
    manual=base.StabilityTests.manual
    def test_legacy_request_timeout_preserves_history(self):
        self.agent.supports_api=True;self.agent.api_url='https://fictional.rtb56.com/api';db.session.commit()
        self.manual();self.login()
        with patch('carrier_http.post',side_effect=TimeoutError('secret')) as remote:
            r=self.post('/track',data={'numbers':'FICTIONAL-001'})
        remote.assert_called_once()
        self.assertIn('API请求失败',r.text)
        self.assertIn('虚构仓库已接收',r.text)
        self.assertEqual(ManualTrack.query.count(),1)

    def test_legacy_number_credentials_and_response_identity(self):
        import views
        from unittest.mock import Mock
        self.agent.supports_api = True
        self.agent.api_url = 'https://fictional.rtb56.com/api'
        self.agent.app_key = 'fake-key'
        self.agent.app_token = 'fake-token'
        db.session.commit()
        payload = {'success': 1, 'data': [{'server_hawbcode': self.shipment.tracking_number,
            'details': [{'track_occur_date': '2026-01-02 12:00:00',
                         'track_location': '虚构港口', 'track_description': '虚构运输'}]}]}
        response = Mock(status_code=200, text=json.dumps(payload))
        with patch('carrier_http.post', return_value=response) as remote:
            events, error = views.fetch_tracking_from_api(self.agent, self.shipment.tracking_number,
                                                         selected_shipment=self.shipment)
        self.assertIsNone(error)
        self.assertEqual(events[0]['description'], '虚构运输')
        sent = remote.call_args.kwargs['data']
        self.assertEqual(sent['appKey'], 'fake-key')
        self.assertEqual(sent['appToken'], 'fake-token')
        self.assertEqual(sent['serviceMethod'], 'gettrack')
        self.assertEqual(json.loads(sent['paramsJson']), {'tracking_number': self.shipment.tracking_number})
        self.assertNotEqual(self.shipment.tracking_number, self.shipment.shipment_id)
        for number in ('OTHER', None, self.shipment.shipment_id):
            payload['data'][0]['server_hawbcode'] = number
            response.text = json.dumps(payload)
            with patch('carrier_http.post', return_value=response):
                events, error = views.fetch_tracking_from_api(self.agent, self.shipment.tracking_number,
                                                             selected_shipment=self.shipment)
            self.assertIsNone(events)
            self.assertIn('身份未通过验证', error)
        with patch('carrier_http.post') as remote:
            events, error = views.fetch_tracking_from_api(self.agent, 'OTHER',
                                                         selected_shipment=self.shipment)
        remote.assert_not_called()
        self.assertIsNotNone(error)


    def test_duplicate_in_agent_account_blocks_before_transport(self):
        import views
        from models import Shipment
        self.agent.supports_api = True
        self.agent.api_url = 'https://fictional.rtb56.com./api'
        db.session.add(Shipment(agent_id=self.agent.id, tracking_number=self.shipment.tracking_number))
        db.session.commit()
        with patch('carrier_http.post') as remote:
            events, error = views.fetch_tracking_from_api(
                self.agent, self.shipment.tracking_number, selected_shipment=self.shipment)
        remote.assert_not_called()
        self.assertIsNone(events)
        self.assertIn('重复查询单号', error)

    def test_safe_diagnostics_distinguish_failures(self):
        import views
        from unittest.mock import Mock
        self.agent.supports_api = True
        self.agent.api_url = 'https://fictional.rtb56.com/api'
        db.session.commit()
        cases = [
            (503, '{}', 'http_error'),
            (200, 'not json secret-token', 'invalid_json'),
            (200, json.dumps({'success': 0}), 'supplier_failure'),
            (200, json.dumps({'success': '1'}), 'success_type:str'),
            (200, json.dumps({'success': 1, 'data': {}}), 'data_structure:dict'),
            (200, json.dumps({'success': 1, 'data': [{}]}), 'identity_missing'),
            (200, json.dumps({'success': 1, 'data': [{'server_hawbcode': 'secret-other'}]}), 'identity_mismatch:str'),
            (200, json.dumps({'success': 1, 'data': []}), 'empty_data_unverified'),
            (200, json.dumps({'success': 1, 'data': [{'server_hawbcode': self.shipment.tracking_number,
                                                     'details': 'secret-invalid'}]}), 'structure_or_event_error'),
        ]
        for status, body, category in cases:
            with self.subTest(category=category), patch('carrier_http.post',
                    return_value=Mock(status_code=status, text=body)), self.assertLogs(level='WARNING') as logs:
                events, error = views.fetch_tracking_from_api(
                    self.agent, self.shipment.tracking_number, selected_shipment=self.shipment)
            self.assertIsNone(events)
            self.assertIn(category, error)
            self.assertNotIn('secret-', ''.join(logs.output) + error)
            self.assertNotIn(self.shipment.tracking_number, ''.join(logs.output))


class ReadOnlyPreview(unittest.TestCase):
    setUpClass = classmethod(base.StabilityTests.setUpClass.__func__)
    tearDown = base.StabilityTests.tearDown
    login = base.StabilityTests.login
    post = base.StabilityTests.post
    token = base.StabilityTests.token
    manual = base.StabilityTests.manual

    def setUp(self):
        base.StabilityTests.setUp(self)
        self.agent.supports_api = True
        self.agent.api_url = 'https://fictional.rtb56.com/api'
        self.agent.app_key, self.agent.app_token = 'fictional-key', 'fictional-token'
        db.session.commit()
        self.payload = {'success': 1, 'data': [{'server_hawbcode': 'FICTIONAL-SERVICE-OTHER',
            'track_status': 'NT', 'details': [{'track_occur_date': '2026-01-02 12:00:00',
                'track_location': '虚构节点', 'track_description': '虚构预览事件<script>unsafe</script>'}]}]}

    def response(self):
        from unittest.mock import Mock
        return Mock(status_code=200, text=json.dumps(self.payload))

    def parse(self, preview=True):
        return parse_response('rtb56', 'fictional-source', 200, json.dumps(self.payload),
                              expected_number=self.shipment.tracking_number,
                              allow_unverified_preview=preview)

    def test_mismatch_is_unverified_without_status_or_service_number(self):
        result = self.parse()
        self.assertEqual(result.status, QueryStatus.UNVERIFIED)
        self.assertEqual(len(result.events), 1)
        self.assertIsNone(result.service_number)
        self.assertEqual(result.shipment_status, 'unknown')
        self.assertEqual(result.events[0].status.value, 'unknown')
        self.assertEqual(self.parse(False).status, QueryStatus.ERROR)
        self.assertEqual(self.parse('true').status, QueryStatus.ERROR)

    def test_matching_number_preserves_verified_behavior(self):
        self.payload['data'][0]['server_hawbcode'] = self.shipment.tracking_number
        self.assertEqual(self.parse().status, QueryStatus.SUCCESS)
        self.login()
        with patch('carrier_http.post', return_value=self.response()):
            page = self.post('/track', data={'numbers': self.shipment.tracking_number})
        self.assertIn('虚构预览事件', page.text)
        self.assertNotIn('服务商单号尚未验证归属', page.text)

    def test_invalid_payloads_never_become_preview(self):
        import copy
        original = copy.deepcopy(self.payload)
        cases = [{'success': 0, 'data': original['data']},
                 {'success': '1', 'data': original['data']},
                 {'success': 1, 'data': original['data'] * 2},
                 {'success': 1, 'data': []},
                 {'success': 1, 'data': {}},
                 {'success': 1, 'data': [{'details': original['data'][0]['details']}]},
                 {'success': 1, 'data': [{'server_hawbcode': 123, 'details': []}]},
                 {'success': 1, 'data': [{'server_hawbcode': 'OTHER', 'details': [None]}]},
                 {'success': 1, 'data': [{'server_hawbcode': 'OTHER', 'details': [{'track_description': 123}]}]},
                 {'success': 1, 'error': 'supplier failed', 'data': original['data']}]
        for payload in cases:
            self.payload = payload
            result = self.parse()
            self.assertFalse(result.events)
            self.assertNotEqual(result.status, QueryStatus.SUCCESS)

    def test_preview_page_has_warning_preserves_manual_history_and_performs_no_writes(self):
        from sqlalchemy import event
        self.manual()
        self.login()
        before_status = self.shipment.status
        statements = []
        def record(conn, cursor, statement, parameters, context, many):
            statements.append(statement)
        event.listen(db.engine, 'before_cursor_execute', record)
        try:
            with patch('carrier_http.post', return_value=self.response()) as remote, \
                 patch('views.sync_tracking_to_supabase') as sync, \
                 patch('views.simple_sync_tracking') as simple, \
                 patch('views.complete_sync_tracking') as complete:
                page = self.post('/track', data={'numbers': self.shipment.tracking_number})
                remote.assert_called_once()
                sync.assert_not_called()
                simple.assert_not_called()
                complete.assert_not_called()
                sent = remote.call_args.kwargs['data']
                self.assertEqual(sent['appKey'], 'fictional-key')
                self.assertEqual(sent['appToken'], 'fictional-token')
                self.assertEqual(json.loads(sent['paramsJson'])['tracking_number'], self.shipment.tracking_number)
        finally:
            event.remove(db.engine, 'before_cursor_execute', record)
        self.assertEqual(page.status_code, 200)
        self.assertIn('服务商单号尚未验证归属', page.text)
        self.assertIn('只读预览', page.text)
        self.assertIn('虚构仓库已接收', page.text)
        self.assertIn('虚构预览事件&lt;script&gt;', page.text)
        self.assertNotIn('<script>unsafe</script>', page.text)
        self.assertFalse(any(s.lstrip().split()[0].upper() in
                             {'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP'}
                             for s in statements))
        self.assertEqual(self.shipment.status, before_status)
        self.assertEqual(ManualTrack.query.count(), 1)

    def test_refresh_rejects_unverified_result_and_never_syncs(self):
        import views
        self.login()
        with patch('carrier_http.post', return_value=self.response()), \
             patch('views.simple_sync_tracking') as sync, \
             patch('views.sync_tracking_to_supabase') as external:
            tracks, error = views._refresh_query(self.shipment, self.agent)
            self.assertIsNone(tracks)
            self.assertIsNotNone(error)
            self.post(f'/shipments/{self.shipment.id}/refresh')
            sync.assert_not_called()
            external.assert_not_called()

    def test_preview_rejects_duplicate_or_inactive_agent_before_request(self):
        import views
        from models import Shipment
        self.agent.is_active = False
        db.session.commit()
        with patch('carrier_http.post') as remote:
            tracks, error = views.fetch_tracking_from_api(
                self.agent, self.shipment.tracking_number, selected_shipment=self.shipment,
                read_only_preview=True)
        remote.assert_not_called()
        self.assertIsNone(tracks)
        self.agent.is_active = True
        db.session.add(Shipment(agent_id=self.agent.id, tracking_number=self.shipment.tracking_number))
        db.session.commit()
        with patch('carrier_http.post') as remote:
            tracks, error = views.fetch_tracking_from_api(
                self.agent, self.shipment.tracking_number, selected_shipment=self.shipment,
                read_only_preview=True)
        remote.assert_not_called()
        self.assertIsNone(tracks)

    def test_cannot_enable_preview_for_safe_refresh_caller(self):
        import views
        with patch('carrier_http.post', return_value=self.response()):
            tracks, error = views.fetch_tracking_from_api(
                self.agent, self.shipment.tracking_number, selected_shipment=self.shipment,
                read_only_preview=True, safe_errors=True)
        self.assertIsNone(tracks)
        self.assertIsNotNone(error)
