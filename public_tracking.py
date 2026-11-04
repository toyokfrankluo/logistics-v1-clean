"""Mode A public boundary. No production transport until quotas/protocol are approved.

Old call_gettrack/call_17track remain for legacy characterization, never called here.
Only sanitized values enter the bounded cache. All state is process-local; production
requires an independently enforced trusted gateway limit before deployment.
"""
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime
import re
import json
import ipaddress
import threading
import time

from flask import current_app, request, jsonify, render_template_string
from models import Shipment, ManualTrack
from tracking import parse_response, QueryStatus

DENIED = '无法查询该运单，请核对单号或联系发件方'
UNAVAILABLE = '物流查询暂不可用'
NOTICE = '物流事件已记录，详细内容暂不公开'
VERSION = 'public-v1'
NUMBER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,49}\Z')
ENDPOINTS = {'public_track_page', 'public_track_json', 'api_track_one', 'api_track_by_agent', 'api_17track'}


class PublicState:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.counts = OrderedDict()
        self.cache = OrderedDict()
        self.calls = deque()

    def charge(self, ip, weight, minute, hour, capacity=10000):
        with self.lock:
            now = self.clock()
            for key in list(self.counts):
                q = self.counts[key]
                while q and q[0][0] <= now - 3600:
                    q.popleft()
                if not q:
                    del self.counts[key]
            if ip not in self.counts and len(self.counts) >= capacity:
                return False  # Fail closed, never evict active counters.
            q = self.counts.setdefault(ip, deque())
            if (sum(n for t, n in q if t > now - 60) + weight > minute
                    or sum(n for _, n in q) + weight > hour):
                return False
            q.append((now, weight))
            return True


def state():
    return current_app.extensions['public_tracking']


def valid_number(value):
    return isinstance(value, str) and bool(NUMBER.fullmatch(value))


def client_ip():
    """Only explicitly trusted socket peers can supply a sanitized forwarding chain.

    Deployment must prevent direct access bypassing the trusted gateway. Walk from
    right to left; never trust a client-prepended address before the first untrusted hop.
    Configuration is code-owned, not an environment variable or request parameter.
    """
    peer = request.remote_addr or 'unknown'
    try:
        networks = [ipaddress.ip_network(n) for n in current_app.config['PUBLIC_TRUSTED_PROXIES']]
        address = ipaddress.ip_address(peer)
        if not any(address in net for net in networks):
            return peer
        raw = request.headers.get('X-Forwarded-For', '')
        if len(raw) > 1024:
            return peer
        chain = [ipaddress.ip_address(n.strip()) for n in raw.split(',') if n.strip()]
        for address in reversed(chain):
            if not any(address in net for net in networks):
                return str(address)
        return peer
    except ValueError:
        return peer


def init_app(app):
    app.extensions['public_tracking'] = PublicState()
    for key, value in dict(PUBLIC_RATE_MINUTE=10, PUBLIC_RATE_HOUR=100,
                           PUBLIC_BATCH_MAX=30, PUBLIC_CACHE_TTL=300,
                           PUBLIC_CACHE_CAPACITY=256, PUBLIC_SUPPLIER_HOURLY=0,
                           PUBLIC_SIMULATION=False, PUBLIC_TRUSTED_PROXIES=()).items():
        app.config.setdefault(key, value)

    @app.before_request
    def protect_public():
        if request.endpoint not in ENDPOINTS:
            return None
        if request.method == 'HEAD':
            return '', 200
        # Use socket peer only. Deliberately ignore X-Forwarded-For and Forwarded.
        weight = 1
        if request.endpoint == 'public_track_page' and request.method == 'POST':
            if request.content_length and request.content_length > 16384:
                return jsonify(error='请求过大'), 413
            numbers = list(dict.fromkeys(request.form.get('numbers', '').split()))
            weight = max(1, sum(valid_number(n) for n in numbers))
            if len(numbers) > app.config['PUBLIC_BATCH_MAX']:
                return jsonify(error='单次查询数量超限'), 429
        if request.content_length and request.content_length > 16384:
            return jsonify(error='请求过大'), 413
        if not state().charge(client_ip(), weight,
                              app.config['PUBLIC_RATE_MINUTE'], app.config['PUBLIC_RATE_HOUR']):
            response = jsonify(error='查询频率超限，请稍后重试')
            response.status_code = 429
            response.headers['Retry-After'] = '60'
            return response

    @app.after_request
    def public_headers(response):
        if request.endpoint in ENDPOINTS:
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
        return response


def failure(number, message=DENIED):
    return {'order_id': number if valid_number(number) else '', 'error': message,
            'status': 'unknown', 'status_text': '状态待确认', 'events': [], 'tracks': []}


def success(number, events):
    return {'order_id': number, 'error': None, 'status': 'unknown',
            'status_text': '状态待确认', 'events': events,
            'tracks': '\n\n'.join(NOTICE + ('\n' + e['time'] if e['time'] else '') for e in events) or '暂无轨迹'}


def public_event(when):
    # Do not publish arbitrary descriptions or locations, even if they look benign.
    return {'time': when, 'location': '', 'description': NOTICE, 'status': 'unknown'}


def select(number, agent_id=None, carrier_id=None, third=False):
    if not valid_number(number):
        return None
    column = Shipment.third_party_tracking_number if third else Shipment.tracking_number
    matches = Shipment.query.filter(column == number).limit(2).all()
    # Ambiguity is global: an attacker cannot disambiguate customers by guessing an agent.
    if len(matches) != 1:
        return None
    s = matches[0]
    if (agent_id is not None and s.agent_id != agent_id
            or carrier_id is not None and s.carrier_id != carrier_id):
        return None
    if not valid_number(s.tracking_number):
        return None
    if third and (not valid_number(s.third_party_tracking_number) or s.third_party_tracking_number != number):
        return None
    return s


def query(number, agent_id=None, carrier_id=None, third=False, prefer_third=True):
    try:
        s = select(number, agent_id, carrier_id, third)
        if s is None:
            return failure(number)
        agent = s.agent
        # History scope remains old manual-only-agent / agentless behavior.
        use_third = third or bool(prefer_third and s.third_party_tracking_number)
        manual_allowed = not use_third and (agent is None or agent.supports_api is False)
        if agent is not None and agent.is_active is not True:
            return failure(number, UNAVAILABLE)
        if s.agent_id is not None and agent is None:
            return failure(number)
        if manual_allowed:
            rows = ManualTrack.query.filter_by(shipment_id=s.id).order_by(ManualTrack.happen_time.desc(), ManualTrack.id.desc()).all()
            if rows:
                return success(s.tracking_number, [public_event(r.happen_time.isoformat(sep=' ') if isinstance(r.happen_time, datetime) else None) for r in rows])
            if agent is not None:
                return success(s.tracking_number, [])
        # Provider identity and number derived only from this selected record.
        provider = '17track' if use_third else None
        supplier_number = s.third_party_tracking_number if provider else s.tracking_number
        if not provider:
            url = (agent.api_url or '').lower() if agent else ''
            if 'nextsls' in url:
                provider, supplier_number = 'nextsls', s.shipment_id
            elif 'rtb56' in url:
                provider = 'rtb56'
            elif 'txfba' in url:
                provider = 'txfba'
            else:
                return failure(number, UNAVAILABLE)  # Unknown protocol / env carrier blocked.
        if not valid_number(supplier_number):
            return failure(number, UNAVAILABLE)
        if agent is not None and agent.supports_api is not True:
            return failure(number, UNAVAILABLE)
        source = (agent.id, agent.updated_at, agent.api_url) if agent else ('third-party',)
        # Hash source configuration; never cache URLs, tokens or credentials.
        import hashlib
        fingerprint = hashlib.sha256(repr(source).encode()).hexdigest()
        key = (s.id, s.customer_id, s.agent_id, s.tracking_number, provider, fingerprint, supplier_number, VERSION)
        st = state()
        # Serialize cache fill: concurrent requests cannot fetch the same response twice.
        with st.lock:
            now = st.clock()
            # There is intentionally no production transport in this release.
            from isolated_runtime import shadow_runtime_valid
            if (current_app.config.get('PUBLIC_SIMULATION') is not True
                    or not shadow_runtime_valid(current_app._get_current_object())):
                return failure(number, UNAVAILABLE)
            cached = st.cache.get(key)
            if cached and cached[0] > now:
                st.cache.move_to_end(key)
                return deepcopy(cached[1])
            provider_fn = current_app.extensions.get('public_simulated_response')
            quota = current_app.config['PUBLIC_SUPPLIER_HOURLY']
            while st.calls and st.calls[0] <= now - 3600:
                st.calls.popleft()
            if not callable(provider_fn) or type(quota) is not int or quota <= len(st.calls):
                return failure(number, UNAVAILABLE)
            st.calls.append(now)
            # Callback installed by trusted test/bootstrap code, never HTTP or environment.
            http_status, body = provider_fn(provider, supplier_number, s)
            parsed = parse_response(provider, str(s.id), http_status=http_status, body=json.dumps(body) if isinstance(body, (dict, list)) else body)
            if parsed.status not in (QueryStatus.SUCCESS, QueryStatus.EMPTY):
                return failure(number, UNAVAILABLE)
            events = []
            for event in parsed.events:
                t = event.time.occurred_at or event.time.local_time
                when = event.time.raw if event.time.precision.value == 'day' else None
                if isinstance(t, datetime):
                    when = t.date().isoformat() if event.time.precision.value == 'day' else t.isoformat(sep=' ', timespec='minutes' if event.time.precision.value == 'minute' else 'auto')
                events.append(public_event(when))
            result = success(s.tracking_number, events)
            st.cache[key] = (now + current_app.config['PUBLIC_CACHE_TTL'], deepcopy(result))
            while len(st.cache) > current_app.config['PUBLIC_CACHE_CAPACITY']:
                st.cache.popitem(last=False)
            return result
    except Exception:
        current_app.logger.warning('public_tracking: query_failed')
        return failure(number, UNAVAILABLE)


def json_lookup():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('order_id'), str) or not data['order_id'].strip():
        return jsonify(error='缺少有效 order_id 参数'), 400
    return jsonify(query(data['order_id'].strip()))


def api_lookup(number, **kwargs):
    result = query(number, prefer_third=False, **kwargs)
    # Explicit safe replacement of legacy supplier envelope; no cnmessage passthrough.
    return jsonify(success='0' if result['error'] else '1', error=result['error'],
                   order_id=result['order_id'], status=result['status'], status_text=result['status_text'],
                   data=[{'details': [{'track_occur_date': e['time'], 'track_location': e['location'],
                                      'track_description': e['description']} for e in result['events']]}])


def page():
    numbers = list(dict.fromkeys(request.form.get('numbers', '').split())) if request.method == 'POST' else []
    results = [query(n) for n in numbers]
    return render_template_string('''<h2>物流轨迹查询</h2><form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<textarea name="numbers" rows="6" placeholder="每行一个单号（最多30条）"></textarea><button>查询</button></form>
{% for r in results %}<h4>{{ r.order_id }}</h4><p>{{ r.status_text }}</p><div style="white-space:pre-wrap">{{ r.error or r.tracks }}</div>{% endfor %}''', results=results)
