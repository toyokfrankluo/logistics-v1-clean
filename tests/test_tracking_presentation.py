import unittest
from tracking.presentation import present_legacy
from tracking.service_types import *
from tracking.adapters import parse_response
from test_tracking_service import response


class PresentationTests(unittest.TestCase):
    def outcome(self, value=None):
        r = value or response()
        parsed = parse_response('rtb56', 'agent:fake', r.http_status, r.body)
        return QueryOutcome(Status(parsed.status.value), 'FAKE-CUSTOMER',
                            sources=(SourceOutcome(parsed.source_id, Status(parsed.status.value), parsed),), selected=parsed)
    def test_public_json_exact(self):
        p = present_legacy(self.outcome(), 'public_json')
        self.assertTrue(p.compatible)
        self.assertEqual(p.payload, {'order_id': 'FAKE-CUSTOMER', 'error': None, 'tracks': '虚构地点 — 虚构事件\n2026-01-02 10:00:00'})
    def test_html_result_fields(self):
        p = present_legacy(self.outcome(), 'public_html')
        self.assertEqual(p.payload, {'error': None, 'tracks': '虚构地点 — 虚构事件\n2026-01-02 10:00:00'})
    def test_backend_text(self):
        self.assertEqual(present_legacy(self.outcome(), 'backend').payload, {'tracks': '2026-01-02 10:00:00 虚构地点 虚构事件', 'error': None})
    def test_order_duplicates_long_text(self):
        events = [{'track_description': x} for x in ('B'*2000, 'A', 'A')]
        p = present_legacy(self.outcome(response(events)), 'public_json')
        self.assertEqual(p.payload['tracks'], 'B'*2000+'\n\nA\n\nA')
    def test_unknown_time_not_invented(self):
        self.assertEqual(present_legacy(self.outcome(response([{'track_description':'虚构'}])), 'public_json').payload['tracks'], '虚构')
    def test_public_empty_incompatible(self): self.assertFalse(present_legacy(self.outcome(response([])), 'public_json').compatible)
    def test_backend_empty(self): self.assertEqual(present_legacy(self.outcome(response([])), 'backend').payload['tracks'], '暂无轨迹')
    def test_unverified_incompatible(self): self.assertFalse(present_legacy(QueryOutcome(Status.UNVERIFIED, 'FAKE'), 'public_json').compatible)
    def test_raw_envelope_incompatible(self): self.assertFalse(present_legacy(self.outcome(), 'raw_api').compatible)
    def test_failure_message_mapping_not_fabricated(self):
        p = present_legacy(QueryOutcome(Status.ERROR, 'FAKE', error='timeout'), 'public_json')
        self.assertFalse(p.compatible)
        self.assertIsNone(p.payload)
    def test_nextsls_text_not_fabricated(self):
        from tracking.types import QueryResult, QueryStatus
        o = QueryOutcome(Status.SUCCESS, 'FAKE', selected=QueryResult('nextsls', 'fake', QueryStatus.SUCCESS))
        self.assertFalse(present_legacy(o, 'public_json').compatible)
    def test_manual_backend_minute_format(self):
        from tracking.types import TrackingEvent
        from tracking.time import parse_time
        e = TrackingEvent('manual', 'manual:fake', parse_time('2026-01-01 10:12:34'), '虚构地点', '虚构手工')
        o = QueryOutcome(Status.SUCCESS, 'FAKE', manual=(ManualEvent(1, e),),
                         sources=(SourceOutcome('external', Status.NOT_REQUESTED),))
        self.assertEqual(present_legacy(o, 'backend').payload['tracks'], '2026-01-01 10:12 虚构地点 虚构手工')
        self.assertFalse(present_legacy(o, 'public_json').compatible)


class PresentationSafetyTests(unittest.TestCase):
    def setUp(self):
        from dataclasses import replace
        self.replace = replace
        self.good = PresentationTests().outcome()
    def reject(self, outcome):
        p = present_legacy(outcome, 'backend')
        self.assertFalse(p.compatible)
        self.assertIsNone(p.payload)
        self.assertTrue(p.incompatibilities)
    def test_selected_missing_source(self): self.reject(self.replace(self.good, sources=()))
    def test_source_identity_mismatch(self):
        s = self.good.sources[0]
        self.reject(self.replace(self.good, sources=(self.replace(s, source_id='other'),)))
    def test_selected_different_events(self):
        r = self.good.selected
        changed = self.replace(r, events=r.events * 2)
        self.reject(self.replace(self.good, selected=changed))
    def test_unique_source_error_top_success(self):
        self.reject(self.replace(self.good, selected=None, sources=(SourceOutcome('agent:fake', Status.ERROR, error='timeout'),)))
    def test_selected_success_error(self):
        from tracking.types import ErrorType
        r = self.replace(self.good.selected, error=ErrorType.BUSINESS)
        self.reject(self.replace(self.good, selected=r, sources=(SourceOutcome(r.source_id, Status.SUCCESS, r, 'business'),)))
    def test_invalid_top_statuses(self):
        for state in ('success', 'unknown', None, 1, {}, Status.NOT_REQUESTED):
            with self.subTest(state=state): self.reject(self.replace(self.good, status=state))
    def test_unverified_promotion(self):
        from tracking.types import QueryStatus, ErrorType
        r = self.replace(self.good.selected,status=QueryStatus.UNVERIFIED,error=ErrorType.UNCONFIRMED_PROTOCOL)
        self.reject(self.replace(self.good,selected=r,sources=(SourceOutcome(r.source_id,Status.UNVERIFIED,r,'unconfirmed_protocol'),)))
    def test_unverified_prefix_not_merged(self):
        self.reject(self.replace(self.good,sources=(SourceOutcome('candidate',Status.UNVERIFIED),*self.good.sources)))
    def test_empty_with_manual_keeps_independent_status(self):
        from tracking.time import parse_time
        from tracking.types import TrackingEvent
        r = parse_response('rtb56','agent:fake',200,response([]).body)
        m = ManualEvent(1,TrackingEvent('manual','manual:1',parse_time('2026-01-01 09:00:00'),'虚构地点','虚构历史'))
        o=QueryOutcome(Status.SUCCESS,'FAKE',manual=(m,),sources=(SourceOutcome(r.source_id,Status.EMPTY,r),),selected=r)
        before=repr(o)
        p=present_legacy(o,'backend')
        self.assertTrue(p.compatible);self.assertIn('虚构历史',p.payload['tracks'])
        self.assertEqual(o.selected.status.value,'empty');self.assertEqual(repr(o),before)
    def test_approved_failure_then_success(self):
        o=self.replace(self.good,sources=(SourceOutcome('third:fake',Status.ERROR,error='timeout'),*self.good.sources))
        self.assertEqual(present_legacy(o,'backend').payload,present_legacy(self.good,'backend').payload)
        self.assertEqual(o.sources[0].error,'timeout')
    def test_parallel_success_blocked(self):
        r=self.good.selected
        other=self.replace(r,source_id='other',events=tuple(self.replace(e,source_id='other') for e in r.events))
        self.reject(self.replace(self.good,sources=(SourceOutcome('other',Status.SUCCESS,other),*self.good.sources)))
    def test_later_failure_blocked(self):
        self.reject(self.replace(self.good,sources=(*self.good.sources,SourceOutcome('later',Status.ERROR,error='timeout'))))
    def test_duplicate_source(self): self.reject(self.replace(self.good,sources=self.good.sources*2))
    def test_local_empty_mode(self):
        o=QueryOutcome(Status.EMPTY,'FAKE',sources=(SourceOutcome('external',Status.NOT_REQUESTED),))
        self.assertEqual(present_legacy(o,'backend').payload,{'tracks':'暂无轨迹','error':None})
    def test_local_mode_marker_missing(self): self.reject(QueryOutcome(Status.EMPTY,'FAKE'))
    def test_source_result_missing(self):
        self.reject(self.replace(self.good,sources=(SourceOutcome('agent:fake',Status.SUCCESS),)))
    def test_unknown_event_status_not_guessed(self):
        r=self.good.selected
        for state in ('delivered','in_transit','unknown'):
            bad=self.replace(r,events=(self.replace(r.events[0],status=state),))
            self.reject(self.replace(self.good,selected=bad,sources=(SourceOutcome(bad.source_id,Status.SUCCESS,bad),)))
    def test_txfba_normal_format(self):
        from tracking.types import LogisticsStatus
        r=self.good.selected
        tx=self.replace(r,provider='txfba',events=tuple(self.replace(e,provider='txfba') for e in r.events))
        o=self.replace(self.good,selected=tx,sources=(SourceOutcome(tx.source_id,Status.SUCCESS,tx),))
        for contract in ('backend','public_json','public_html'):
            self.assertEqual(present_legacy(o,contract).payload,present_legacy(self.good,contract).payload)
        self.assertEqual(tx.events[0].status,LogisticsStatus.UNKNOWN)
    def test_malformed_structure_controlled(self):
        for o in (None,{},self.replace(self.good,customer_number=None),self.replace(self.good,customer_number=''),self.replace(self.good,sources=[self.good.sources[0]]),self.replace(self.good,manual=('bad',))):
            self.reject(o)
    def test_event_source_mismatch(self):
        r=self.good.selected
        bad=self.replace(r,events=(self.replace(r.events[0],source_id='other'),))
        self.reject(self.replace(self.good,selected=bad,sources=(SourceOutcome(bad.source_id,Status.SUCCESS,bad),)))
