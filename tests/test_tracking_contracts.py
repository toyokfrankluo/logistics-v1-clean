"""3A characterization: assertions describe current behavior, including defects."""
import copy
import contextlib
import io
import unittest
from unittest.mock import Mock, patch

import test_v1 as baseline  # Enables disposable DB + network denial before app import.
import test_round2 as safety
import views
from jinja2 import TemplateNotFound
from requests import Timeout
from models import db, ManualTrack
from public_tracking import NOTICE, DENIED, UNAVAILABLE

EVENT = {"track_occur_date": "2026-01-02 10:00:00", "track_location": "虚构港口", "track_description": "虚构接口事件"}
SUCCESS = {"success": "1", "cnmessage": "虚构成功", "data": [{"details": [EVENT]}]}
NEXTSLS = {"status": 1, "data": {"shipment": {"traces": [{"time": "2026-01-02 10:00:00", "location": "虚构港口", "info": "虚构接口事件"}]}}}


def response(payload=None, status=200, invalid=False):
    result = Mock(status_code=status, text="fictional raw response")
    if invalid:
        result.json.side_effect = ValueError("fictional invalid JSON")
    else:
        result.json.return_value = copy.deepcopy(payload)
    return result


class TrackingContracts(unittest.TestCase):
    setUpClass = classmethod(baseline.StabilityTests.setUpClass.__func__)
    tearDown = baseline.StabilityTests.tearDown
    token = baseline.StabilityTests.token
    post = baseline.StabilityTests.post
    login = baseline.StabilityTests.login
    manual = baseline.StabilityTests.manual

    def setUp(self):
        baseline.StabilityTests.setUp(self)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.http = patch("requests.post", side_effect=AssertionError("Unconfigured HTTP POST"))
        self.http_post = self.http.start()
        self.addCleanup(self.http.stop)
        carrier_mock = patch("carrier_http.post", self.http_post)
        carrier_mock.start()
        self.addCleanup(carrier_mock.stop)
        for method in ("get", "patch", "put", "delete"):
            guard = patch("requests." + method, side_effect=AssertionError("Unexpected external request"))
            guard.start()
            self.addCleanup(guard.stop)
        self.agent.supports_api = True
        self.agent.api_url = "https://fictional.rtb56.com/api"
        self.agent.app_key = "fictional-key"
        self.agent.app_token = "fictional-token"
        db.session.commit()

    def public(self):
        return self.client.post("/public_track", json={"order_id": "FICTIONAL-001"})

    def test_json_success_exact_contract(self):
        self.http_post.side_effect = None
        self.http_post.return_value = response(SUCCESS)
        result = self.public()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["tracks"], NOTICE + "\n2026-01-02 10:00:00")
        self.assertIsNone(result.json["error"])
        self.assertEqual(result.json["status"], "unknown")

    def test_json_missing_and_unknown_number_contract(self):
        self.assertEqual(self.client.post('/public_track', json={}).status_code, 400)
        result = self.client.post('/public_track', json={"order_id": "UNKNOWN"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["error"], DENIED)
        self.assertEqual(result.json["tracks"], [])
        self.http_post.assert_not_called()

    def test_api_routes_use_safe_envelope(self):
        self.http_post.side_effect = None
        self.http_post.return_value = response(SUCCESS)
        for path in ('/api/track/fictional/FICTIONAL-001', f'/api/track_by_agent/{self.agent.id}/FICTIONAL-001'):
            with self.subTest(path=path):
                result = self.client.get(path)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.mimetype, 'application/json')
                self.assertEqual(result.json["data"][0]["details"][0], {"track_occur_date": "2026-01-02 10:00:00", "track_location": "", "track_description": NOTICE})
                self.assertNotIn("cnmessage", result.json)

    def test_public_web_and_frontend_contract(self):
        self.http_post.side_effect = None
        self.http_post.return_value = response(SUCCESS)
        result = self.post('/public/track', data={"numbers": "FICTIONAL-001"})
        self.assertEqual(result.status_code, 200)
        self.assertIn(NOTICE, result.get_data(as_text=True))
        frontend = self.client.get('/frontend').get_data(as_text=True)
        self.assertIn('fetch("/public_track"', frontend)
        self.assertIn('textContent', frontend)

    def test_backend_success_and_supplier_payload(self):
        self.agent.api_url = "https://fictional.txfba.com/api"
        db.session.commit()
        self.login()
        self.http_post.side_effect = None
        self.http_post.return_value = response(SUCCESS)
        result = self.post('/track', data={"numbers": "FICTIONAL-001"})
        self.assertEqual(result.status_code, 200)
        self.assertIn('2026-01-02 10:00:00 虚构港口 虚构接口事件', result.get_data(as_text=True))
        self.assertEqual(self.http_post.call_args.kwargs['data']['serviceMethod'], 'gettrack')
        self.assertEqual(ManualTrack.query.count(), 0)
        self.assertEqual(self.shipment.status, '已录入')

    def test_empty_tracks_are_success_with_different_outputs(self):
        self.http_post.side_effect = None
        self.http_post.return_value = response({"success": "1", "cnmessage": "虚构空轨迹", "data": [{"details": []}]})
        self.assertEqual(self.public().json['tracks'], '暂无轨迹')
        self.login()
        text = self.post('/track', data={"numbers": "FICTIONAL-001"}).get_data(as_text=True)
        self.assertIn('暂无轨迹', text)

    def test_timeout_preserves_manual_but_public_does_not_fallback(self):
        self.manual()
        self.http_post.side_effect = Timeout('fictional timeout')
        result = self.public()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['tracks'], [])
        self.assertEqual(result.json['error'], UNAVAILABLE)
        self.login()
        text = self.post('/track', data={"numbers": "FICTIONAL-001"}).get_data(as_text=True)
        self.assertIn('虚构仓库已接收', text)
        self.assertIn('API请求失败', text)
        self.assertEqual(ManualTrack.query.count(), 1)

    def test_http_error_differs_between_public_and_backend(self):
        self.http_post.side_effect = None
        self.http_post.return_value = response(SUCCESS, status=503)
        self.assertEqual(self.public().json['error'], UNAVAILABLE)  # 4B rejects HTTP failures.
        tracks, error = views.fetch_tracking_from_api(self.agent, 'FICTIONAL-001')
        self.assertIsNone(tracks)
        self.assertEqual(error, 'API返回错误 503')

    def test_invalid_json_public_displays_raw_backend_reports_error(self):
        self.http_post.side_effect = None
        self.http_post.return_value = response(invalid=True)
        self.assertEqual(self.public().json['error'], UNAVAILABLE)
        tracks, error = views.fetch_tracking_from_api(self.agent, 'FICTIONAL-001')
        self.assertIsNone(tracks)
        self.assertEqual(error, 'API请求失败，请检查代理配置或稍后重试')
        self.assertNotIn('fictional invalid JSON', error)

    def test_string_zero_is_not_normalized_as_error(self):
        self.http_post.side_effect = None
        self.http_post.return_value = response({"success": "0", "cnmessage": "虚构拒绝", "data": []})
        self.assertEqual(self.public().json['error'], UNAVAILABLE)
        tracks, error = views.fetch_tracking_from_api(self.agent, 'FICTIONAL-001')
        self.assertEqual((tracks, error), ([], None))

    def test_missing_time_preserved_without_fabrication_in_queries(self):
        payload = copy.deepcopy(SUCCESS)
        del payload['data'][0]['details'][0]['track_occur_date']
        self.http_post.side_effect = None
        self.http_post.return_value = response(payload)
        self.assertEqual(self.public().json['tracks'], NOTICE)
        tracks, error = views.fetch_tracking_from_api(self.agent, 'FICTIONAL-001')
        self.assertIsNone(tracks[0]['time'])
        self.assertIsNone(error)

    def test_duplicate_events_remain_duplicated(self):
        payload = copy.deepcopy(SUCCESS)
        payload['data'][0]['details'].append(copy.deepcopy(EVENT))
        self.http_post.side_effect = None
        self.http_post.return_value = response(payload)
        self.assertEqual(self.public().json['tracks'].count(NOTICE), 2)
        tracks, _ = views.fetch_tracking_from_api(self.agent, 'FICTIONAL-001')
        self.assertEqual(len(tracks), 2)

    def test_nextsls_raw_public_and_normalized_backend(self):
        self.agent.api_url = 'https://fictional.nextsls.com/api'
        db.session.commit()
        self.http_post.side_effect = None
        self.http_post.return_value = response(NEXTSLS)
        result = self.client.get(f'/api/track_by_agent/{self.agent.id}/FICTIONAL-001')
        self.assertNotIn("shipment", result.get_data(as_text=True))
        self.assertIn(NOTICE, self.public().json["tracks"])
        tracks, error = views.fetch_tracking_from_api(self.agent, 'FICTIONAL-001')
        self.assertEqual(tracks, [{"time": "2026-01-02 10:00:00", "location": "虚构港口", "description": "虚构接口事件"}])
        self.assertIsNone(error)

    def test_nextsls_missing_provider_number_differs(self):
        self.agent.api_url = 'https://fictional.nextsls.com/api'
        self.shipment.shipment_id = None
        db.session.commit()
        self.assertEqual(self.public().json['error'], UNAVAILABLE)
        self.http_post.assert_not_called()
        self.http_post.side_effect = None
        self.http_post.return_value = response(NEXTSLS)
        views.fetch_tracking_from_api(self.agent, 'FICTIONAL-001')
        self.assertEqual(self.http_post.call_args.kwargs['json']['shipment']['shipment_id'], 'FICTIONAL-001')

    def test_17track_api_and_public_precedence(self):
        self.shipment.third_party_tracking_number = 'FICTIONAL-THIRD'
        db.session.commit()
        self.http_post.side_effect = None
        self.http_post.return_value = response({"status": 200, "data": [{"track": [{"time": "T", "location": "L", "description": "虚构第三方"}]}]})
        with patch.object(baseline.backend, 'TRACK17_API_KEY', 'fictional-only'):
            api = self.client.get('/api/17track/FICTIONAL-THIRD')
            self.assertEqual(api.json['data'][0]['details'][0], {'track_occur_date': None, 'track_location': '', 'track_description': NOTICE})
            self.assertEqual(self.public().json['tracks'], NOTICE)


    def test_17track_failure_modes(self):
        cases = [(response(status=503), '返回错误 503'), (response({"status": 500, "message": "fictional denied"}), 'fictional denied'), (response(invalid=True), 'fictional invalid JSON')]
        with patch.object(baseline.backend, 'TRACK17_API_KEY', 'fictional-only'):
            for mock_response, expected in cases:
                self.http_post.side_effect = None
                self.http_post.return_value = mock_response
                result = self.client.get('/api/17track/FAKE')
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json['error'], DENIED)
                self.http_post.assert_not_called()
            self.http_post.side_effect = Timeout('fictional timeout')
            self.assertEqual(self.client.get('/api/17track/FAKE').json['error'], DENIED)
            self.http_post.assert_not_called()

    def test_17track_failure_falls_back_to_agent(self):
        self.shipment.third_party_tracking_number = 'FICTIONAL-THIRD'
        db.session.commit()
        self.http_post.side_effect = [Timeout('fictional timeout'), response(SUCCESS)]
        with patch.object(baseline.backend, 'TRACK17_API_KEY', 'fictional-only'):
            self.assertEqual(self.public().json['error'], UNAVAILABLE)
        self.assertEqual(self.http_post.call_count, 1)  # No unapproved provider fallback.

    def test_broken_17track_template_is_recorded(self):
        self.login()
        with self.assertRaises(TemplateNotFound) as caught:
            self.client.get('/track_17track')
        self.assertEqual(caught.exception.name, 'track_17track.html')

    def test_queries_role_matrix(self):
        self.agent.supports_api = False
        db.session.commit()
        self.manual()
        for role in ('anonymous', 'member', 'admin'):
            self.client = self.app.test_client()
            if role != 'anonymous':
                self.login(admin=role == 'admin')
            for path in ('/frontend', '/public/track', '/api/track/x/FICTIONAL-001', f'/api/track_by_agent/{self.agent.id}/FICTIONAL-001'):
                self.assertEqual(self.client.get(path).status_code, 200, (role, path))
            self.assertEqual(self.public().status_code, 200)
            self.assertEqual(self.client.get('/track').status_code, 302 if role == 'anonymous' else 200)
        self.http_post.assert_not_called()

    def test_all_refresh_role_matrix_without_remote_writes(self):
        for role, expected in (('anonymous', 302), ('member', 403), ('admin', 302)):
            self.client = self.app.test_client()
            if role != 'anonymous':
                self.login(admin=role == 'admin')
            with patch('views.fetch_tracking_from_api', return_value=([], None)), patch('views.requests.post') as remote:
                remote.return_value.status_code = 201
                with patch.dict('os.environ', {'SUPABASE_URL': 'https://fictional.invalid', 'SUPABASE_KEY': 'fictional'}):
                    for path in safety.SYNC_PATHS:
                        self.assertEqual(self.post(path).status_code, expected, (role, path))
                self.assertEqual(remote.call_count, 1 if role == 'admin' else 0)

    def test_failed_refresh_does_not_remove_manual_history(self):
        self.login()
        self.manual()
        with patch('views.fetch_tracking_from_api', return_value=(None, 'fictional timeout')):
            self.assertEqual(self.post('/shipments/1/refresh').status_code, 302)
        self.assertEqual(ManualTrack.query.count(), 1)

    def test_complete_history_failure_returns_zero(self):
        self.assertFalse(hasattr(self.shipment, 'tracks'))
        self.assertEqual(views.complete_sync_tracking(self.shipment, [{"time": "T", "description": "虚构事件"}]), 0)
        self.assertEqual(ManualTrack.query.count(), 0)

    def test_error_cache_reuses_failure(self):
        self.http_post.side_effect = Timeout('fictional timeout')
        self.assertTrue(self.public().json['error'])
        self.http_post.side_effect = None
        self.http_post.return_value = response(SUCCESS)
        self.assertIsNone(self.public().json['error'])
        self.assertEqual(self.http_post.call_count, 2)  # Failures never poison the cache.
