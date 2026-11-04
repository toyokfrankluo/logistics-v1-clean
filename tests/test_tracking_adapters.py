"""Pure adapter tests: fabricated JSON only, no Flask fixtures or DB imports."""
import contextlib
import io
import json
import logging
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tracking import parse_response, QueryStatus, ErrorType, LogisticsStatus, TimePrecision
from tracking.time import parse_time

PROVIDERS = ('rtb56', 'txfba', 'nextsls', '17track', 'generic')


def payload(provider, count=1):
    if provider in ('rtb56', 'txfba'):
        return {'success': '1', 'data': [{'details': [dict(track_occur_date='2026-01-02 10:20:30', track_location='虚构港口', track_description='虚构轨迹') for _ in range(count)]}]}
    events = [dict(time='2026-01-02T10:20:30+08:00', location='虚构港口', description='虚构轨迹', info='虚构轨迹') for _ in range(count)]
    if provider == 'nextsls':
        return {'status': 1, 'data': {'shipment': {'traces': events}}}
    if provider == '17track':
        return {'status': 200, 'data': [{'track': events}]}
    return {'code': 200, 'tracks': events}


def events_of(provider, data):
    if provider in ('rtb56', 'txfba'):
        return data['data'][0]['details']
    if provider == 'nextsls':
        return data['data']['shipment']['traces']
    if provider == '17track':
        return data['data'][0]['track']
    return data['tracks']


def parse(provider, data, status=200, **kwargs):
    return parse_response(provider, 'fictional-agent:1', status, json.dumps(data), **kwargs)


class AdapterTests(unittest.TestCase):
    def test_independent_import_without_framework_or_io(self):
        script = '''import sys,socket
sys.path.insert(0, sys.argv[1])
def denied(*a,**k): raise AssertionError('network')
socket.socket.connect=denied
import tracking
assert 'flask' not in sys.modules and 'sqlalchemy' not in sys.modules and 'requests' not in sys.modules
assert tracking.parse_response('rtb56','fake',200,'{"success":"1","data":[{"details":[]}]}').status.value == 'empty'
'''
        subprocess.run([sys.executable, '-I', '-B', '-c', script, str(Path(__file__).resolve().parents[1])], check=True, capture_output=True)

    def test_no_logging_or_raw_envelope_retention(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                for provider in PROVIDERS:
                    data = payload(provider)
                    data['appToken'] = 'FICTIONAL-PRIVATE-SECRET'
                    data['message'] = 'FICTIONAL-PRIVATE-SECRET'
                    result = parse(provider, data)
                    self.assertNotIn('FICTIONAL-PRIVATE-SECRET', repr(result))
                    bad = parse(provider, {'error': 'FICTIONAL-PRIVATE-SECRET'})
                    self.assertNotIn('FICTIONAL-PRIVATE-SECRET', repr(bad))
            self.assertEqual(stream.getvalue(), '')
        finally:
            logging.getLogger().removeHandler(handler)

    def test_naive_time_never_uses_machine_timezone(self):
        t = parse_time('2026-01-02 10:20:30')
        self.assertIsNone(t.occurred_at)
        self.assertIsNone(t.timezone)
        self.assertEqual(t.local_time, datetime(2026, 1, 2, 10, 20, 30))
        self.assertEqual(t.issue, 'timezone_unconfirmed')

    def test_explicit_timezone_converts_to_utc(self):
        t = parse_time('2026-01-02T10:20:30+08:00')
        self.assertEqual(t.occurred_at, datetime(2026, 1, 2, 2, 20, 30, tzinfo=timezone.utc))

    def test_configured_timezone(self):
        t = parse_time('2026-01-02 10:20:30', source_timezone='Asia/Singapore')
        self.assertEqual(t.occurred_at.hour, 2)
        self.assertEqual(t.timezone, 'Asia/Singapore')

    def test_date_only_does_not_invent_midnight(self):
        t = parse_time('2026-01-02')
        self.assertIsNone(t.occurred_at)
        self.assertIsNone(t.local_time)
        self.assertEqual(t.precision, TimePrecision.DAY)

    def test_precision(self):
        for text, precision in [('2026-01-02T10:20Z', TimePrecision.MINUTE), ('2026-01-02T10:20:30Z', TimePrecision.SECOND), ('2026-01-02T10:20:30.123Z', TimePrecision.SUBSECOND)]:
            self.assertEqual(parse_time(text).precision, precision)

    def test_unknown_and_invalid_times(self):
        for raw in (None, '', 'yesterday', '2026-02-31', True, {}, float('inf')):
            self.assertIsNone(parse_time(raw).occurred_at)
        self.assertEqual(parse_time(0).issue, 'numeric_time_unit_unconfirmed')
        self.assertEqual(parse_time(0, epoch_seconds=True).occurred_at.year, 1970)

    def test_dst_ambiguity_not_guessed(self):
        for raw in ('2026-11-01 01:30:00', '2026-03-08 02:30:00'):
            t = parse_time(raw, source_timezone='America/New_York')
            self.assertIsNone(t.occurred_at)
            self.assertEqual(t.issue, 'ambiguous_or_nonexistent_local_time')

    def test_generic_without_success_evidence_is_unverified(self):
        for shape in ({'tracks': [{'description': '虚构轨迹'}]}, {'data': [{'description': '虚构轨迹'}]}, {'result': {'list': [{'description': '虚构轨迹'}]}}):
            result = parse('generic', shape)
            self.assertEqual(result.status, QueryStatus.UNVERIFIED)
            self.assertEqual(result.error, ErrorType.UNCONFIRMED_PROTOCOL)

    def test_generic_error_before_tracks(self):
        self.assertEqual(parse('generic', {'code': 500, 'tracks': [{'description': '虚构'}]}).error, ErrorType.BUSINESS)

    def test_nextsls_alternate_shapes_explicitly_pending(self):
        for shape in ({'tracks': [{'info': '虚构'}]}, {'data': [{'description': '虚构'}]}):
            shape['status'] = 1
            result = parse('nextsls', shape)
            self.assertEqual(result.status, QueryStatus.SUCCESS)
            self.assertIn('alternate_nextsls_shape_unconfirmed', result.pending_confirmation)

    def test_nextsls_epoch_seconds(self):
        data = payload('nextsls')
        events_of('nextsls', data)[0]['time'] = 0
        self.assertEqual(parse('nextsls', data).events[0].time.occurred_at.year, 1970)

    def test_unmapped_event_ids_and_status_do_not_get_guessed(self):
        data = payload('generic')
        data['tracks'][0].update(id='FAKE-ID', status='delivered', description='已签收')
        event = parse('generic', data).events[0]
        self.assertEqual(event.status, LogisticsStatus.UNKNOWN)
        self.assertIsNone(event.external_event_id)


def provider_case(provider, scenario):
    def test(self):
        data = payload(provider)
        if scenario == 'success':
            result = parse(provider, data)
            self.assertEqual(result.status, QueryStatus.SUCCESS)
            self.assertEqual(result.events[0].provider, provider)
            self.assertEqual(result.events[0].source_id, 'fictional-agent:1')
        elif scenario == 'empty':
            self.assertEqual(parse(provider, payload(provider, 0)).status, QueryStatus.EMPTY)
        elif scenario == 'multiple':
            result = parse(provider, payload(provider, 3))
            self.assertEqual(len(result.events), 3)  # No premature deduplication.
        elif scenario == 'http_error':
            self.assertEqual(parse(provider, data, 503).error, ErrorType.HTTP)
        elif scenario == 'invalid_json':
            for body in ('<html>', '{', 'NaN', 'null', '[]'):
                result = parse_response(provider, 'fake', 200, body)
                self.assertEqual(result.status, QueryStatus.ERROR)
        elif scenario == 'business_error':
            key = 'success' if provider in ('rtb56', 'txfba') else ('code' if provider == 'generic' else 'status')
            for bad in ('0', 0, False, 'false', None, 500, 1.0):
                data[key] = bad
                self.assertEqual(parse(provider, data).error, ErrorType.BUSINESS, (provider, bad))
        elif scenario == 'missing_fields':
            self.assertEqual(parse(provider, {}).status, QueryStatus.ERROR)
            events_of(provider, data)[0].clear()
            self.assertEqual(parse(provider, data).error, ErrorType.SCHEMA)
        elif scenario == 'missing_time':
            event = events_of(provider, data)[0]
            event.pop('track_occur_date' if provider in ('rtb56', 'txfba') else 'time')
            t = parse(provider, data).events[0].time
            self.assertIsNone(t.occurred_at)
            self.assertIsNone(t.raw)
        elif scenario == 'long_description':
            event = events_of(provider, data)[0]
            key = 'track_description' if provider in ('rtb56', 'txfba') else ('info' if provider == 'nextsls' else 'description')
            event[key] = '虚构完整描述' * 2000
            self.assertEqual(parse(provider, data).events[0].description, event[key])
        elif scenario == 'malformed_event':
            events_of(provider, data).append('wrong')
            result = parse(provider, data)
            self.assertEqual(result.error, ErrorType.SCHEMA)
            self.assertEqual(result.events, ())  # No silent partial success.
    return test


for provider in PROVIDERS:
    for scenario in ('success', 'empty', 'multiple', 'http_error', 'invalid_json', 'business_error', 'missing_fields', 'missing_time', 'long_description', 'malformed_event'):
        setattr(AdapterTests, 'test_' + provider + '_' + scenario, provider_case(provider, scenario))
