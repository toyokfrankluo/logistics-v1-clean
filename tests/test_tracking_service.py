"""All inputs are fictional; no application or database imports."""
import json
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch
from tracking.service_types import *
from tracking.service import resolve_query, query_tracking
from tracking.time import parse_time
from tracking.types import TrackingEvent


class MemoryRepository:
    def __init__(self, *rows): self.rows = rows
    def by_id(self, key): return next((s for s in self.rows if s.local_id == key), None)
    def by_customer_number(self, key): return tuple(s for s in self.rows if s.customer_number == key)


class FakeResponses:
    def __init__(self, *values): self.values, self.calls = list(values), []
    def fetch(self, attempt):
        self.calls.append(attempt)
        value = self.values.pop(0)
        if isinstance(value, Exception): raise value
        return value


def response(events=None, **envelope):
    return Response(200, json.dumps({'success': '1', 'data': [{'details': events if events is not None else [
        {'track_occur_date': '2026-01-02 10:00:00', 'track_location': '虚构地点', 'track_description': '虚构事件'}]}], **envelope}))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.agent = Source('agent:fake', 'rtb56', 'customer', verified=True)
        event = TrackingEvent('manual', 'manual:1', parse_time('2026-01-02 09:00:00'), '', '虚构手工')
        self.manual = ManualEvent(1, event)
        self.shipment = ShipmentSnapshot(7, 'FAKE-CUSTOMER', 'FAKE-PROVIDER', 'FAKE-THIRD', self.agent, manual=(self.manual,))
    def plan(self, shipment=None, audience='backend', **kwargs):
        return resolve_query(QueryRequest(audience, local_id=7, **kwargs), MemoryRepository(shipment or self.shipment))
    def run_query(self, value, shipment=None):
        transport = FakeResponses(value)
        outcome = query_tracking(self.plan(shipment), transport)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(outcome.manual, (self.manual,))
        return outcome
    def test_local_primary_key(self):
        self.assertEqual(self.plan().shipment.local_id, 7)
    def test_ambiguous_customer(self):
        plan = resolve_query(QueryRequest('public', customer_number='FAKE-CUSTOMER'), MemoryRepository(self.shipment, replace(self.shipment, local_id=8)))
        transport = FakeResponses()
        self.assertEqual(query_tracking(plan, transport).error, 'ambiguous_identity')
        self.assertEqual(transport.calls, [])
    def test_invalid_identity(self):
        self.assertEqual(resolve_query(QueryRequest('public', local_id=7, customer_number='x'), MemoryRepository()).blocked_reason, 'invalid_identity')
    def test_not_found(self):
        self.assertEqual(resolve_query(QueryRequest('public', local_id=8), MemoryRepository()).blocked_reason, 'not_found')
    def test_nextsls_provider_number(self):
        s = replace(self.shipment, agent=Source('next:fake', 'nextsls', 'provider', True))
        self.assertEqual(self.plan(s).attempts[0].number, 'FAKE-PROVIDER')
    def test_nextsls_missing_number_no_request(self):
        s = replace(self.shipment, provider_number=None, agent=Source('next:fake', 'nextsls', 'provider', True))
        t = FakeResponses()
        result = query_tracking(self.plan(s), t)
        self.assertEqual((result.status, result.error), (Status.BLOCKED, 'missing_query_number'))
        self.assertEqual((result.manual, t.calls), ((self.manual,), []))
    def test_customer_number_for_rtb_and_txfba(self):
        for provider in ('rtb56', 'txfba'):
            self.assertEqual(self.plan(replace(self.shipment, agent=replace(self.agent, provider=provider))).attempts[0].number, 'FAKE-CUSTOMER')
    def test_inactive_preserves_history(self):
        t = FakeResponses()
        o = query_tracking(self.plan(replace(self.shipment, agent=replace(self.agent, active=False))), t)
        self.assertEqual((o.status, o.manual, t.calls), (Status.BLOCKED, (self.manual,), []))
    def test_unverified_configuration_no_call(self):
        t = FakeResponses()
        o = query_tracking(self.plan(replace(self.shipment, agent=replace(self.agent, verified=False))), t)
        self.assertEqual((o.error, t.calls), ('protocol_unverified', []))
    def test_number_policy_mismatch(self):
        self.assertEqual(self.plan(replace(self.shipment, agent=replace(self.agent, number_kind='provider'))).attempts[0].blocked_reason, 'number_policy_mismatch')
    def test_generic_not_enabled_by_verified_flag(self):
        self.assertEqual(self.plan(replace(self.shipment, agent=replace(self.agent, provider='generic'))).attempts[0].blocked_reason, 'unsupported_protocol')
    def test_local_only(self):
        t = FakeResponses()
        o = query_tracking(self.plan(local_only=True), t)
        self.assertEqual((o.status, o.sources[0].status, t.calls), (Status.SUCCESS, Status.NOT_REQUESTED, []))
    def test_local_empty(self):
        self.assertEqual(query_tracking(self.plan(replace(self.shipment, manual=()), local_only=True), FakeResponses()).status, Status.EMPTY)
    def test_success_unknown_status(self):
        o = self.run_query(response())
        self.assertEqual(o.status, Status.SUCCESS)
        self.assertEqual(o.selected.events[0].status.value, 'unknown')
        self.assertIn('supplier_protocol_not_verified', o.selected.pending_confirmation)
    def test_empty(self):
        o = self.run_query(response([]))
        self.assertEqual(o.status, Status.SUCCESS)
        self.assertEqual(o.sources[0].status, Status.EMPTY)
    def test_timeout(self): self.assertEqual(self.run_query(TimeoutError('secret')).error, 'timeout')
    def test_http(self): self.assertEqual(self.run_query(Response(503, '{}')).error, 'http')
    def test_json(self): self.assertEqual(self.run_query(Response(200, 'bad-json')).error, 'invalid_json')
    def test_business_string_zero(self): self.assertEqual(self.run_query(response(success='0')).error, 'business')
    def test_business_integer_zero(self): self.assertEqual(self.run_query(response(success=0)).error, 'business')
    def test_schema(self): self.assertEqual(self.run_query(Response(200, '{"success":"1"}')).error, 'schema')
    def test_exception_redacted(self):
        o = self.run_query(RuntimeError('secret-token'))
        self.assertNotIn('secret-token', repr(o))
    def test_unknown_time_duplicates_full_description(self):
        event = {'track_description': '虚构' * 1000}
        o = self.run_query(response([event, event]))
        self.assertEqual(len(o.selected.events), 2)
        self.assertIsNone(o.selected.events[0].time.occurred_at)
        self.assertEqual(o.selected.events[0].description, event['track_description'])
    def test_only_duplicate_manual_id_collapsed(self):
        s = replace(self.shipment, manual=(self.manual, self.manual, replace(self.manual, record_id=2)))
        o = query_tracking(self.plan(s, local_only=True), FakeResponses())
        self.assertEqual([m.record_id for m in o.manual], [1, 2])
    def test_public_backend_source_order(self):
        s = replace(self.shipment, third_party=Source('third:fake', '17track', 'third_party', True))
        self.assertEqual([a.source.provider for a in self.plan(s, 'public').attempts], ['17track', 'rtb56'])
        self.assertEqual([a.source.provider for a in self.plan(s).attempts], ['rtb56'])
    def test_public_failure_fallback_exactly_two_requests(self):
        s = replace(self.shipment, third_party=Source('third:fake', '17track', 'third_party', True))
        t = FakeResponses(Response(503, '{}'), response())
        o = query_tracking(self.plan(s, 'public'), t)
        self.assertEqual((o.status, len(t.calls)), (Status.SUCCESS, 2))
        self.assertEqual(o.sources[0].error, 'http')
        self.assertEqual(t.calls[0].number, 'FAKE-THIRD')
    def test_public_empty_does_not_fallback(self):
        s = replace(self.shipment, third_party=Source('third:fake', '17track', 'third_party', True))
        t = FakeResponses(Response(200, '{"status":200,"data":[{"track":[]}]}'))
        self.assertEqual(query_tracking(self.plan(s, 'public'), t).status, Status.SUCCESS)
        self.assertEqual(len(t.calls), 1)
    def test_unverified_not_promoted_or_retried(self):
        from tracking.types import QueryResult, QueryStatus
        with patch('tracking.service.parse_response', return_value=QueryResult('rtb56', 'agent:fake', QueryStatus.UNVERIFIED)):
            self.assertEqual(self.run_query(response()).status, Status.UNVERIFIED)
    def test_no_runtime_imports_or_network(self):
        code = '''import socket, sqlite3, sys
from unittest.mock import patch
with patch.object(socket.socket, 'connect', side_effect=AssertionError('network')), patch.object(sqlite3, 'connect', side_effect=AssertionError('database')):
 import tracking.service, tracking.presentation
assert not any(x in sys.modules for x in ('flask', 'sqlalchemy', 'requests', 'supabase', 'app', 'views'))
'''
        subprocess.run([sys.executable, '-B', '-c', code], check=True)
    def test_forged_plan_cannot_bypass_source_policy(self):
        source = replace(self.agent, active=False)
        plan = QueryPlan('backend', self.shipment, (Attempt(source, 'FAKE-CUSTOMER'),))
        t = FakeResponses()
        self.assertEqual(query_tracking(plan, t).status, Status.BLOCKED)
        self.assertEqual(t.calls, [])
    def test_forged_query_number_blocked(self):
        plan = QueryPlan('backend', self.shipment, (Attempt(self.agent, 'OTHER'),))
        t = FakeResponses()
        self.assertEqual(query_tracking(plan, t).error, 'query_number_mismatch')
        self.assertEqual(t.calls, [])
    def test_query_does_not_write_or_connect(self):
        import socket
        import sqlite3
        import builtins
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network')), patch.object(sqlite3, 'connect', side_effect=AssertionError('database')), patch.object(builtins, 'open', side_effect=AssertionError('file IO')):
            o = self.run_query(response())
        self.assertEqual(o.status, Status.SUCCESS)
        self.assertEqual(self.shipment.manual, (self.manual,))
