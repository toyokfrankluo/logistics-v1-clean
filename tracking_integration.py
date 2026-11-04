"""Only a single fictional RTB56 shadow. No HTTP client or production provider."""
from flask import current_app, request
from tracking.service_types import Source, ShipmentSnapshot, QueryRequest, Response
from tracking.service import resolve_query, query_tracking


def _diagnostic(category):
    # Logging failure must not destroy an already valid old result.
    try:
        current_app.logger.info('tracking_shadow_auto_text:%s', category)
    except Exception:
        pass


def _load_old_parser():
    from comparison.runner import run_legacy
    return run_legacy


def _compare_auto_text(plan, captured, expected):
    class Replay:
        def fetch(self, attempt): return captured
    outcome = query_tracking(plan, Replay())
    from tracking.presentation import present_legacy
    new = present_legacy(outcome, 'backend')
    return 'auto_text_equal' if new.compatible and new.payload['tracks'] == expected else 'auto_text_difference'


def simulated_track():
    try:
        return _simulated_track()
    except Exception:
        _diagnostic('legacy_unavailable')
        return ({}, '模拟查询失败，未生成有效旧结果')


def _simulated_track():
    mode = current_app.config.get('TRACKING_MODE', 'legacy')
    if mode == 'legacy' or mode not in {'shadow', 'service'}:
        return None
    # A config key is not authorization. Only a disposable isolated runtime qualifies.
    if mode == 'service' or current_app.config.get('ISOLATED_MODE') is not True:
        return ({}, '当前环境禁止所选追踪模式')
    from isolated_runtime import shadow_runtime_valid, is_shadow_fixture
    app = current_app._get_current_object()
    if not shadow_runtime_valid(app):
        return ({}, '当前环境禁止所选追踪模式')
    from flask_login import current_user
    if not current_user.is_admin:
        return ({}, '模拟对比仅供管理员使用')
    numbers = request.form.get('numbers', '').strip().splitlines()
    if len(numbers) != 1 or not numbers[0].strip():
        return ({}, '模拟对比仅支持单票查询')
    from models import Shipment, db
    no = numbers[0].strip()
    with db.session.no_autoflush:
        rows = Shipment.query.filter_by(tracking_number=no).all()
        if len(rows) != 1:
            return ({}, '运单不存在或身份不唯一')
        s = rows[0]
        if ((request.form.get('customer_id') and str(s.customer_id) != request.form['customer_id'])
                or (request.form.get('agent_id') and str(s.agent_id) != request.form['agent_id'])):
            return ({}, '筛选条件与运单不一致')
        agent = s.agent
        # Only the seeded fictional identity is eligible; real credentials/URL never used.
        if (not is_shadow_fixture(app, s) or s.tracking_number != 'DEMO-001' or not agent or not agent.is_active
                or not agent.supports_api):
            return ({}, '仅允许已启用代理的虚构 DEMO-001 运单')
        manual = [f"{e.happen_time.strftime('%Y-%m-%d %H:%M')} {e.location or ''} {e.description}" for e in s.manual_tracks]
        snapshot = ShipmentSnapshot(s.id, no, agent=Source('simulation:rtb56', 'rtb56', 'customer', True))
    class Repository:
        def by_id(self, key): return snapshot if key == snapshot.local_id else None
    class FictionalProvider:
        calls = 0
        def fetch(self, attempt):
            self.calls += 1
            if self.calls != 1: raise RuntimeError('duplicate fetch')
            return Response(200, '{"success":"1","data":[{"details":[{"track_occur_date":"2026-01-02 10:00:00","track_location":"虚构地点","track_description":"虚构模拟轨迹"}]}]}')
    provider = FictionalProvider()
    plan = resolve_query(QueryRequest('backend', local_id=snapshot.local_id), Repository())
    # Exactly one acquisition, then the same immutable response for both parsers.
    captured = provider.fetch(plan.attempts[0])
    old = _load_old_parser()('backend', 'rtb56', captured.http_status, captured.body)
    if (type(old) is not dict or old.get('exception') is not None
            or old.get('status') != 'no_explicit_error' or old.get('error') is not None
            or type(old.get('events')) is not list):
        raise ValueError('Invalid legacy result')
    auto_lines = []
    for e in old['events']:
        if type(e) is not dict or any(k not in e or (e[k] is not None and type(e[k]) is not str)
                                     for k in ('time', 'location', 'description')):
            raise ValueError('Invalid legacy event')
        auto_lines.append(f"{e['time'] or ''} {e['location'] or ''} {e['description'] or ''}")
    lines = manual + auto_lines
    payload = {no: {'tracks': '\n'.join(lines) or '暂无轨迹', 'error': None}}
    try:
        category = _compare_auto_text(plan, captured, '\n'.join(auto_lines) or '暂无轨迹')
    except Exception:
        category = 'auto_text_comparison_error'
    _diagnostic(category)
    return payload, '虚构 RTB56 单票模拟对比完成'
