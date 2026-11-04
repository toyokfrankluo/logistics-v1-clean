import unittest
from dataclasses import replace
import test_tracking_service as helpers
from test_tracking_service import MemoryRepository, FakeResponses, response
from tracking.service_types import *
from tracking.service import resolve_query, query_tracking
from tracking.presentation import present_legacy
from tracking.types import QueryResult, QueryStatus


class Boundaries(unittest.TestCase):
    setUp = helpers.ServiceTests.setUp
    plan = helpers.ServiceTests.plan
    def blocked(self, plan):
        t = FakeResponses()
        self.assertEqual(query_tracking(plan, t).status, Status.BLOCKED)
        self.assertEqual(t.calls, [])
    def test_other_agent(self):
        self.blocked(replace(self.plan(), attempts=(Attempt(replace(self.agent, source_id='other'), 'FAKE-CUSTOMER'),)))
    def test_duplicate_attempt(self): self.blocked(replace(self.plan(), attempts=self.plan().attempts * 2))
    def test_missing_attempt(self): self.blocked(replace(self.plan(), attempts=()))
    def test_invalid_audience(self): self.blocked(replace(self.plan(), audience='invalid'))
    def test_public_wrong_order(self):
        s = replace(self.shipment, third_party=Source('third', '17track', 'third_party', True))
        p = self.plan(s, 'public')
        self.blocked(replace(p, attempts=p.attempts[::-1]))
    def test_flags(self):
        for v in ('true', 'false', 0, 1, None):
            with self.subTest(v=v): self.blocked(self.plan(replace(self.shipment, agent=replace(self.agent, verified=v))))
    def test_wrong_repository_id(self):
        class Wrong:
            def by_id(_self, key): return replace(self.shipment, local_id=99)
        self.blocked(resolve_query(QueryRequest('backend', local_id=7), Wrong()))
    def test_wrong_repository_number(self):
        class Wrong:
            def by_customer_number(_self, key): return (self.shipment,)
        self.blocked(resolve_query(QueryRequest('backend', customer_number='OTHER'), Wrong()))
    def test_empty_history(self):
        o = query_tracking(self.plan(), FakeResponses(response([])))
        self.assertEqual((o.status, o.sources[0].status, o.manual), (Status.SUCCESS, Status.EMPTY, (self.manual,)))
    def test_presentation_not_requested(self): self.assertFalse(present_legacy(QueryOutcome(Status.NOT_REQUESTED, 'fake'), 'backend').compatible)
    def test_presentation_contradiction(self):
        for status in (QueryStatus.ERROR, QueryStatus.UNVERIFIED):
            o = QueryOutcome(Status.SUCCESS, 'fake', selected=QueryResult('rtb56', 'fake', status))
            self.assertFalse(present_legacy(o, 'backend').compatible)
    def test_unapproved_failure_history_incompatible(self):
        o = query_tracking(self.plan(), FakeResponses(TimeoutError()))
        self.assertEqual(o.manual, (self.manual,))
        self.assertFalse(present_legacy(o, 'backend').compatible)
