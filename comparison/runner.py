"""Execute selected ACTUAL legacy helper ASTs with read-only in-memory doubles.

No import of app/views, no Flask context/ORM, no HTTP client. Never accepts live
credentials or fetches a response. Output is for synthetic fixtures only.
"""
import ast
import contextlib
import io
import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from tracking import parse_response

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = {'app.py': ('call_gettrack', 'call_17track', 'format_tracks_from_data'),
             'views.py': ('fetch_tracking_from_api', 'fetch_17track_tracking')}


def _load(filename, namespace):
    tree = ast.parse((ROOT / filename).read_text(), filename=filename)
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS[filename]]
    if len(selected) != len(FUNCTIONS[filename]):
        raise RuntimeError('Legacy function inventory changed; review harness')
    for node in selected:
        if node.decorator_list or any(isinstance(child, (ast.Import, ast.ImportFrom)) for child in ast.walk(node)):
            raise RuntimeError('Legacy helper now has imports/decorators; review isolation')
    exec(compile(ast.Module(body=selected, type_ignores=[]), filename, 'exec'), namespace)


class ReadOnlyQuery:
    def __init__(self, value):
        self.value = value
    def filter_by(self, **kwargs):
        return self
    def first(self):
        return self.value
    def get(self, ident):
        return self.value


class FixtureResponse:
    def __init__(self, body, status):
        self.text, self.status_code = body, status
    def json(self):
        return json.loads(self.text)


def run_legacy(entry, provider, http_status, body, *, provider_number='FAKE-PROVIDER', active=True):
    if entry not in ('public', 'backend'):
        raise ValueError('Unknown entry')
    calls = []
    agent = SimpleNamespace(id=1, is_active=active, supports_api=True, app_key='fake', app_token='fake',
                            username='', password='', api_url='https://fictional.' + provider + '.com/api')
    shipment = SimpleNamespace(id=1, tracking_number='FAKE-CUSTOMER', shipment_id=provider_number,
                               agent_id=1, manual_tracks=[])
    def post(url, **kwargs):
        # Deliberate offline boundary; url is merely captured, never contacted.
        calls.append({'url': url, 'data': kwargs.get('data'), 'json': kwargs.get('json')})
        return FixtureResponse(body, http_status)
    namespace = dict(json=json, time=time, datetime=datetime, requests=SimpleNamespace(post=post),
                     Shipment=SimpleNamespace(query=ReadOnlyQuery(shipment)),
                     CarrierAgent=SimpleNamespace(query=ReadOnlyQuery(agent)), CACHE={}, CACHE_TTL=600,
                     CARRIERS={}, CARRIERS_LIST=[], DEFAULT_API_URL='', TRACK17_API_KEY='fake', TRACK17_API_URL='https://fictional.invalid')
    _load('app.py' if entry == 'public' else 'views.py', namespace)
    error = None
    events = None
    text = None
    exception = None
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            if entry == 'public':
                raw = namespace['call_17track']('FAKE-THIRD') if provider == '17track' else namespace['call_gettrack'](None, 'FAKE-CUSTOMER', agent_id=1)
                error = raw.get('error') if isinstance(raw, dict) else None
                text = namespace['format_tracks_from_data'](raw)
                # Only project structures the old formatter actually understands.
                if isinstance(raw, dict) and isinstance(raw.get('data'), list) and raw['data'] and 'details' in raw['data'][0]:
                    events = [dict(time=e.get('track_occur_date'), location=e.get('track_location'), description=e.get('track_description', e.get('track_description_en'))) for e in raw['data'][0]['details']]
                elif error:
                    events = []
            else:
                raw, error = namespace['fetch_17track_tracking']('FAKE-THIRD') if provider == '17track' else namespace['fetch_tracking_from_api'](agent, 'FAKE-CUSTOMER')
                if isinstance(raw, list):
                    events = [dict(time=e.get('time', e.get('happen_time')), location=e.get('location'), description=e.get('description', e.get('status'))) for e in raw]
                elif isinstance(raw, str):
                    text = raw  # Do not reconstruct events from a presentation string.
                elif error:
                    events = []
        except Exception as exc:
            exception = type(exc).__name__
    return dict(status='exception' if exception else ('error' if error else 'no_explicit_error'),
                error=error, exception=exception, events=events, text=text, requests=calls)


def compare(case):
    new = parse_response(case['provider'], 'fictional-agent:1', case['http_status'], case['body'])
    projected = [dict(time=e.time.raw, location=e.location, description=e.description) for e in new.events]
    comparisons = {}
    for entry in ('public', 'backend'):
        old = run_legacy(entry, case['provider'], case['http_status'], case['body'])
        old_events = old['events']
        comparisons[entry] = dict(old=old, differences={
            'event_count': None if old_events is None else len(old_events) != len(projected),
            'ordered_events': None if old_events is None else old_events != projected,
            'times': None if old_events is None else [e['time'] for e in old_events] != [e['time'] for e in projected],
            'locations': None if old_events is None else [e['location'] for e in old_events] != [e['location'] for e in projected],
            'descriptions': None if old_events is None else [e['description'] for e in old_events] != [e['description'] for e in projected],
        })
    return dict(name=case['name'], provider=case['provider'], classification=classify(case['name']), new=asdict(new), entries=comparisons,
                note='null comparison means legacy did not expose structured events; not equivalence')


def classify(name):
    """Review labels, never an instruction to change a route automatically."""
    if name in {'http-error', 'invalid-json', 'business-zero-str', 'business-zero-int', 'generic-explicit-error'}:
        return {'category': 'old_defect', 'recommendation': 'Reject supplier/transport failure; presentation/status-code changes need approval'}
    if name in {'missing-time', 'missing-description', 'nextsls-epoch', 'generic-unverified'}:
        return {'category': 'protocol_unconfirmed', 'recommendation': 'Confirm missing-field/time/success semantics; preserve unknown/unverified'}
    return {'category': 'compatibility', 'recommendation': 'Preserve envelope/text, event multiplicity and order; do not silently normalize public output'}
