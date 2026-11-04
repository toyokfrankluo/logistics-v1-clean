"""4B: fictional records, isolated SQLite, injected responses; never real HTTP."""
import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import test_v1 as baseline
from models import db, Shipment, Customer, CarrierAgent, ManualTrack
from public_tracking import PublicState, DENIED, UNAVAILABLE, NOTICE

BODY = {'success': '1', 'data': [{'details': [
    {'track_occur_date': '2026-01-02 10:00:00', 'track_location': '客户甲家庭地址电话123456789',
     'track_description': '客户甲 电话123456789 地址某街99号 密钥SECRET'}]}], 'secret': 'SECRET'}


class PublicSecurity(unittest.TestCase):
    setUpClass = classmethod(baseline.StabilityTests.setUpClass.__func__)
    tearDown = baseline.StabilityTests.tearDown
    token = baseline.StabilityTests.token
    post = baseline.StabilityTests.post
    login = baseline.StabilityTests.login
    manual = baseline.StabilityTests.manual

    def setUp(self):
        baseline.StabilityTests.setUp(self)
        self.agent.supports_api = True
        self.agent.api_url = 'https://fictional.rtb56.com/api'
        db.session.commit()
        self.provider = Mock(return_value=(200, copy.deepcopy(BODY)))
        self.app.extensions['public_simulated_response'] = self.provider

    def lookup(self, number='FICTIONAL-001', **kwargs):
        return self.client.post('/public_track', json={'order_id': number}, **kwargs)

    def duplicate(self, customer=None, agent=None):
        s = Shipment(tracking_number=self.shipment.tracking_number,
                     customer_id=(customer or self.customer).id, agent_id=(agent or self.agent).id)
        db.session.add(s)
        db.session.commit()
        return s

    def test_unique_and_public_whitelist(self):
        result = self.lookup()
        self.assertIsNone(result.json['error'])
        self.assertEqual(set(result.json), {'order_id', 'error', 'status', 'status_text', 'events', 'tracks'})
        self.assertEqual(result.json['status'], 'unknown')
        self.assertEqual(result.json['events'], [{'time': '2026-01-02 10:00:00', 'location': '', 'description': NOTICE, 'status': 'unknown'}])
        self.assertNotIn('SECRET', result.text)
        self.assertNotIn('123456789', result.text)
        self.assertNotIn('客户甲', result.text)
        self.assertEqual(self.provider.call_count, 1)
        self.assertIs(self.provider.call_args.args[2], self.shipment)

    def test_zero_and_multiple_same_response(self):
        zero = self.lookup().json  # prime valid cache before ambiguity
        self.assertIsNone(zero['error'])
        self.duplicate()
        ambiguous = self.lookup().json
        self.assertEqual(ambiguous['error'], DENIED)
        self.assertEqual(self.lookup('MISSING').json['error'], DENIED)
        self.assertEqual(self.provider.call_count, 1)

    def test_cross_customer_duplicate(self):
        c = Customer(name='虚构乙'); db.session.add(c); db.session.commit()
        self.duplicate(customer=c)
        self.assertEqual(self.lookup().json['error'], DENIED)
        self.provider.assert_not_called()

    def test_cross_agent_duplicate_cannot_disambiguate_by_url(self):
        a = CarrierAgent(name='虚构乙代理'); db.session.add(a); db.session.commit()
        self.duplicate(agent=a)
        r = self.client.get(f'/api/track_by_agent/{self.agent.id}/FICTIONAL-001')
        self.assertEqual(r.json['error'], DENIED)
        self.provider.assert_not_called()

    def test_wrong_agent_cannot_hit_valid_cache(self):
        self.lookup()
        r = self.client.get('/api/track_by_agent/999/FICTIONAL-001')
        self.assertEqual(r.json['error'], DENIED)
        self.assertEqual(r.json['data'], [{'details': []}])
        self.assertEqual(self.provider.call_count, 1)

    def test_wrong_carrier_zero_calls(self):
        self.assertEqual(self.client.get('/api/track/wrong/FICTIONAL-001').json['error'], DENIED)
        self.provider.assert_not_called()

    def test_inactive_cannot_use_cache_or_fetch(self):
        self.lookup()
        self.agent.is_active = False; db.session.commit()
        self.assertEqual(self.lookup().json['error'], UNAVAILABLE)
        self.assertEqual(self.provider.call_count, 1)

    def test_deleted_record_invalidates_cache(self):
        self.lookup()
        db.session.delete(self.shipment); db.session.commit()
        self.assertEqual(self.lookup().json['error'], DENIED)
        self.assertEqual(self.provider.call_count, 1)

    def test_agent_reassignment_invalidates_cache(self):
        self.lookup()
        a = CarrierAgent(name='虚构替代', supports_api=True, api_url='https://fictional.rtb56.com/api')
        db.session.add(a); db.session.flush(); self.shipment.agent_id = a.id; db.session.commit()
        db.session.expire_all()
        self.lookup()
        self.assertEqual(self.provider.call_count, 2)

    def test_cache_ttl_capacity_and_no_sensitive_text(self):
        st = self.app.extensions['public_tracking']
        clock = [0]; st.clock = lambda: clock[0]
        self.lookup(); self.lookup()
        self.assertEqual(self.provider.call_count, 1)
        self.assertNotIn('SECRET', repr(st.cache))
        self.assertNotIn('fictional.rtb56', repr(st.cache))
        clock[0] = 301; self.lookup()
        self.assertEqual(self.provider.call_count, 2)
        self.app.config['PUBLIC_CACHE_CAPACITY'] = 1
        db.session.add(Shipment(tracking_number='SECOND', agent_id=self.agent.id)); db.session.commit()
        self.lookup('SECOND'); self.assertEqual(len(st.cache), 1)

    def test_failure_does_not_overwrite_previous_cache_or_manual(self):
        self.manual(); self.lookup()
        st = self.app.extensions['public_tracking']; before = copy.deepcopy(st.cache)
        st.clock = lambda: 10**12
        self.provider.side_effect = RuntimeError('SECRET URL PATH customer')
        with self.assertLogs(self.app.logger, level='WARNING') as log:
            r = self.lookup()
        self.assertEqual(r.json['error'], UNAVAILABLE)
        self.assertEqual(st.cache, before)
        self.assertEqual(ManualTrack.query.count(), 1)
        self.assertNotIn('SECRET', r.text + str(log.output))

    def test_all_failure_shapes_controlled(self):
        for code, payload in [(503, BODY), (200, {'success': '0', 'cnmessage': 'SECRET'}),
                              (200, 'SECRET'), (200, []), (200, {'unknown': 'SECRET'})]:
            self.provider.return_value = (code, payload)
            self.assertEqual(self.lookup().json['error'], UNAVAILABLE)
        self.assertEqual(self.provider.call_count, 5)

    def test_invalid_json_exception_safe(self):
        self.provider.side_effect = ValueError('SECRET')
        self.assertEqual(self.lookup().json['error'], UNAVAILABLE)
        self.assertEqual(self.provider.call_count, 1)

    def test_event_order_duplicates_unknown_time(self):
        body = copy.deepcopy(BODY)
        body['data'][0]['details'] *= 2
        body['data'][0]['details'].append({'track_description': 'secret', 'track_occur_date': 'not-time'})
        self.provider.return_value = (200, body)
        events = self.lookup().json['events']
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0], events[1])
        self.assertIsNone(events[2]['time'])

    def test_manual_text_safe_without_history_expansion(self):
        self.manual()
        self.agent.supports_api = False; db.session.commit()
        r = self.lookup()
        self.assertIn(NOTICE, r.json['tracks'])
        self.assertNotIn('虚构仓库', r.text)
        self.provider.assert_not_called()

    def test_17track_unregistered_rejected(self):
        self.assertEqual(self.client.get('/api/17track/ARBITRARY').json['error'], DENIED)
        self.provider.assert_not_called()

    def test_17track_only_bound_third_number(self):
        self.shipment.third_party_tracking_number = 'THIRD-001'; db.session.commit()
        self.provider.return_value = (200, {'status': 200, 'data': [{'track': [{'time': '2026-01-02', 'description': 'SECRET'}]}]})
        self.assertEqual(self.client.get('/api/17track/FICTIONAL-001').json['error'], DENIED)
        r = self.client.get('/api/17track/THIRD-001')
        self.assertIsNone(r.json['error'])
        self.assertEqual(r.json['order_id'], 'FICTIONAL-001')
        self.assertEqual(self.provider.call_args.args[:2], ('17track', 'THIRD-001'))

    def test_17track_duplicate_binding_rejected(self):
        self.shipment.third_party_tracking_number = 'THIRD'; db.session.add(Shipment(tracking_number='OTHER', third_party_tracking_number='THIRD')); db.session.commit()
        self.assertEqual(self.client.get('/api/17track/THIRD').json['error'], DENIED)
        self.provider.assert_not_called()

    def test_minute_limit_shared_across_routes_and_spoofed_forwarded(self):
        self.app.config['PUBLIC_RATE_MINUTE'] = 2
        self.lookup(headers={'X-Forwarded-For': '1.2.3.4'})
        self.client.get('/api/track/fictional/FICTIONAL-001')
        r = self.lookup(headers={'X-Forwarded-For': '5.6.7.8'})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.provider.call_count, 1)

    def test_hour_limit_with_clock(self):
        st = self.app.extensions['public_tracking']; clock = [0]; st.clock = lambda: clock[0]
        self.app.config.update(PUBLIC_RATE_MINUTE=1, PUBLIC_RATE_HOUR=2)
        self.assertEqual(self.lookup().status_code, 200); clock[0] = 61
        self.assertEqual(self.lookup().status_code, 200); clock[0] = 122
        self.assertEqual(self.lookup().status_code, 429)

    def test_batch_deduplicates_and_charges_unique(self):
        self.app.config['PUBLIC_RATE_MINUTE'] = 2
        r = self.post('/public/track', data={'numbers': 'FICTIONAL-001\nFICTIONAL-001\nMISSING'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.lookup().status_code, 429)

    def test_batch_over_limit_no_fetch(self):
        r = self.post('/public/track', data={'numbers': '\n'.join('N'+str(i) for i in range(31))})
        self.assertEqual(r.status_code, 429); self.provider.assert_not_called()

    def test_head_zero_calls(self):
        for path in ['/public/track', '/api/track/fictional/FICTIONAL-001', f'/api/track_by_agent/{self.agent.id}/FICTIONAL-001', '/api/17track/THIRD']:
            self.assertEqual(self.client.head(path).status_code, 200)
        self.assertEqual(self.client.head('/public_track').status_code, 405)
        self.provider.assert_not_called()

    def test_cors_and_csrf(self):
        self.assertNotIn('Access-Control-Allow-Origin', self.lookup(headers={'Origin': 'https://evil.invalid'}).headers)
        self.assertEqual(self.client.post('/public/track', data={'numbers': 'FICTIONAL-001'}).status_code, 400)
        self.assertEqual(self.client.get('/track').status_code, 302)

    def test_invalid_numbers_and_body(self):
        for n in ['<script>alert(1)</script>', 'A'*51, 'A/B', 'A?key=SECRET']:
            self.assertEqual(self.lookup(n).json['order_id'], '')
        for body in [[], None, {'order_id': []}]:
            self.assertEqual(self.client.post('/public_track', json=body).status_code, 400)
        self.provider.assert_not_called()

    def test_production_and_forged_isolation_blocked(self):
        self.app.config['PUBLIC_SIMULATION'] = False
        self.assertEqual(self.lookup().json['error'], UNAVAILABLE)
        self.app.config['PUBLIC_SIMULATION'] = True
        with patch('isolated_runtime.shadow_runtime_valid', return_value=False):
            self.assertEqual(self.lookup().json['error'], UNAVAILABLE)
        self.provider.assert_not_called()

    def test_unknown_quota_disables_provider(self):
        self.app.config['PUBLIC_SUPPLIER_HOURLY'] = 0
        self.assertEqual(self.lookup().json['error'], UNAVAILABLE)
        self.provider.assert_not_called()

    def test_supplier_budget_independent_of_client_limit(self):
        self.app.config['PUBLIC_SUPPLIER_HOURLY'] = 1
        self.provider.return_value = (503, {})
        self.lookup(); self.lookup()
        self.assertEqual(self.provider.call_count, 1)

    def test_concurrent_cache_fill_single_provider(self):
        def run(_):
            with self.app.test_client() as client:
                return client.post('/public_track', json={'order_id': 'FICTIONAL-001'}).json
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(8)))
        self.assertTrue(all(r['error'] is None for r in results))
        self.assertEqual(self.provider.call_count, 1)

    def test_concurrent_limit_atomic(self):
        st = PublicState()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: st.charge('peer', 1, 10, 100), range(40)))
        self.assertEqual(sum(results), 10)

    def test_counter_capacity_fails_closed(self):
        st = PublicState()
        self.assertTrue(st.charge('first', 1, 10, 100, capacity=1))
        self.assertFalse(st.charge('second', 1, 10, 100, capacity=1))
        self.assertIn('first', st.counts)

    def test_explicit_trusted_proxy_chain(self):
        self.app.config.update(PUBLIC_RATE_MINUTE=1, PUBLIC_TRUSTED_PROXIES=('127.0.0.1/32',))
        self.assertEqual(self.lookup(headers={'X-Forwarded-For': '192.0.2.1'}).status_code, 200)
        self.assertEqual(self.lookup(headers={'X-Forwarded-For': '192.0.2.2'}).status_code, 200)
        # Prepending an attacker-chosen value does not replace the untrusted final hop.
        self.assertEqual(self.lookup(headers={'X-Forwarded-For': '192.0.2.99,192.0.2.1'}).status_code, 429)

    def test_turning_off_simulation_also_blocks_its_cache(self):
        self.lookup()
        self.app.config['PUBLIC_SIMULATION'] = False
        self.assertEqual(self.lookup().json['error'], UNAVAILABLE)
        self.assertEqual(self.provider.call_count, 1)

    def test_date_precision_not_fabricated(self):
        body = copy.deepcopy(BODY)
        body['data'][0]['details'][0]['track_occur_date'] = '2026-01-02'
        self.provider.return_value = (200, body)
        self.assertEqual(self.lookup().json['events'][0]['time'], '2026-01-02')

    def test_supplier_number_change_invalidates_cache(self):
        self.agent.api_url = 'https://fictional.nextsls.com/api'; db.session.commit()
        self.provider.return_value = (200, {'status': 1, 'data': {'shipment': {'traces': [{'time': '2026-01-02', 'info': 'SECRET'}]}}})
        self.lookup()
        self.shipment.shipment_id = 'DIFFERENT'; db.session.commit()
        self.lookup()
        self.assertEqual(self.provider.call_count, 2)
        self.assertEqual(self.provider.call_args.args[1], 'DIFFERENT')

    def test_concurrent_requests_over_limit_do_not_fetch_twice(self):
        self.app.config['PUBLIC_RATE_MINUTE'] = 2
        def run(_):
            with self.app.test_client() as client:
                return client.post('/public_track', json={'order_id': 'FICTIONAL-001'}).status_code
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(run, range(8)))
        self.assertEqual(statuses.count(200), 2)
        self.assertEqual(statuses.count(429), 6)
        self.assertEqual(self.provider.call_count, 1)

    def test_oversized_request_blocked(self):
        self.assertEqual(self.lookup('A'*17000).status_code, 413)
        self.provider.assert_not_called()

    def test_public_html_never_contains_untrusted_description(self):
        body = copy.deepcopy(BODY)
        body['data'][0]['details'][0]['track_description'] = '<script>SECRET</script>'
        self.provider.return_value = (200, body)
        html = self.post('/public/track', data={'numbers': 'FICTIONAL-001'}).text
        self.assertNotIn('<script>', html)
        self.assertNotIn('SECRET', html)
        self.assertIn(NOTICE, html)

    def test_agent_api_does_not_switch_to_third_party(self):
        self.shipment.third_party_tracking_number = 'THIRD'; db.session.commit()
        r = self.client.get(f'/api/track_by_agent/{self.agent.id}/FICTIONAL-001')
        self.assertIsNone(r.json['error'])
        self.assertEqual(self.provider.call_args.args[:2], ('rtb56', 'FICTIONAL-001'))
        self.assertEqual(set(r.json), {'success', 'error', 'order_id', 'status', 'status_text', 'data'})
