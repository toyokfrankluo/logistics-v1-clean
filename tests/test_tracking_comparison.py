"""Offline comparison characterization, no ORM fixture or app imports."""
import builtins
import contextlib
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from comparison.fixtures import corpus
from comparison.runner import compare, run_legacy

CASES = {case['name']: case for case in corpus()}


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        # Prove comparison itself cannot connect to SQLite or network; no fake DB
        # is created here. All ORM access is replaced by read-only doubles.
        for target in ('sqlite3.connect', 'socket.socket.connect'):
            guard = patch(target, side_effect=AssertionError('Forbidden side effect'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_independent_process_does_not_import_business_or_orm(self):
        code = '''import sys,socket,sqlite3
sys.path.insert(0,sys.argv[1])
def denied(*a,**k): raise AssertionError('side effect')
socket.socket.connect=denied
sqlite3.connect=denied
from comparison.fixtures import corpus
from comparison.runner import compare
for case in corpus(): compare(case)
assert not any(m in sys.modules for m in ('app','views','flask','sqlalchemy','requests'))
'''
        subprocess.run([sys.executable, '-I', '-B', '-c', code, str(Path(__file__).resolve().parents[1])], check=True, capture_output=True)

    def test_no_files_written_or_legacy_logs_emitted(self):
        real_open = builtins.open
        def safe_open(file, mode='r', *args, **kwargs):
            if any(flag in mode for flag in ('w', 'a', '+', 'x')):
                raise AssertionError('file write')
            return real_open(file, mode, *args, **kwargs)
        stream = io.StringIO()
        with patch('builtins.open', side_effect=safe_open), contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            for case in corpus(): compare(case)
        self.assertEqual(stream.getvalue(), '')

    def test_public_nextsls_projection_is_unavailable_not_zero(self):
        result = compare(CASES['nextsls-success'])
        self.assertIsNone(result['entries']['public']['old']['events'])
        self.assertIsNone(result['entries']['public']['differences']['event_count'])
        self.assertEqual(len(result['new']['events']), 1)

    def test_epoch_timezone_difference_is_protocol_question(self):
        result = compare(CASES['nextsls-epoch'])
        self.assertEqual(result['classification']['category'], 'protocol_unconfirmed')
        self.assertEqual(result['new']['events'][0]['time']['occurred_at'].year, 1970)
        self.assertIsNone(result['new']['events'][0]['time']['local_time'])

    def test_order_duplicates_and_long_descriptions_preserved(self):
        for name, count in (('multiple-order', 2), ('duplicate-events', 2), ('long-description', 1)):
            result = compare(CASES[name])
            self.assertEqual(len(result['new']['events']), count)
            for entry in result['entries'].values():
                self.assertFalse(entry['differences']['descriptions'])
                self.assertFalse(entry['differences']['ordered_events'])

    def test_missing_provider_identity_public_rejects_backend_falls_back(self):
        case = CASES['nextsls-success']
        args = (case['provider'], 200, case['body'])
        public = run_legacy('public', *args, provider_number=None)
        backend = run_legacy('backend', *args, provider_number=None)
        self.assertEqual(public['requests'], [])
        self.assertEqual(public['status'], 'error')
        self.assertEqual(backend['requests'][0]['json']['shipment']['shipment_id'], 'FAKE-CUSTOMER')

    def test_disabled_agent_blocked_in_both_entries(self):
        case = CASES['rtb56-success']
        public = run_legacy('public', case['provider'], 200, case['body'], active=False)
        backend = run_legacy('backend', case['provider'], 200, case['body'], active=False)
        self.assertEqual(public['status'], 'error')
        self.assertEqual(public['requests'], [])
        self.assertEqual(backend['status'], 'error')
        self.assertEqual(backend['requests'], [])

    def test_generic_credentials_drive_different_request_shapes(self):
        case = CASES['generic-unverified']
        public = run_legacy('public', 'generic', 200, case['body'])
        backend = run_legacy('backend', 'generic', 200, case['body'])
        self.assertIn('serviceMethod', public['requests'][0]['data'])
        self.assertNotIn('serviceMethod', backend['requests'][0]['data'])

    def test_unknown_never_promoted_to_delivered(self):
        result = compare(CASES['generic-unverified'])
        self.assertEqual(result['new']['status'], 'unverified')
        self.assertEqual(result['new']['events'][0]['status'], 'unknown')
        self.assertIsNone(result['new']['events'][0]['time']['occurred_at'])

    def test_http_and_json_errors_classified_without_rewriting_old(self):
        for name, error in (('http-error', 'http'), ('invalid-json', 'invalid_json')):
            result = compare(CASES[name])
            self.assertEqual(result['new']['error'], error)
            self.assertEqual(result['entries']['public']['old']['status'], 'no_explicit_error')
            self.assertEqual(result['entries']['backend']['old']['status'], 'error')
            self.assertEqual(result['classification']['category'], 'old_defect')


def case_test(case):
    def test(self):
        result = compare(case)
        self.assertIn(result['classification']['category'], ('old_defect', 'compatibility', 'protocol_unconfirmed'))
        self.assertEqual(result['name'], case['name'])
        self.assertIn(result['new']['status'], ('success', 'empty', 'error', 'unverified'))
        for entry in result['entries'].values():
            self.assertIsNone(entry['old']['exception'])
            self.assertIn('times', entry['differences'])
            self.assertIn('locations', entry['differences'])
            self.assertIn('descriptions', entry['differences'])
    return test


for name, case in CASES.items():
    setattr(ComparisonTests, 'test_corpus_' + name.replace('-', '_'), case_test(case))
