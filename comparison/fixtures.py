"""Synthetic response corpus. No captured production data."""
import json


def corpus():
    event = {'track_occur_date': '2026-01-02 10:00:00', 'track_location': '虚构港口', 'track_description': '虚构完整描述'}
    def rtb(events, success='1'):
        return {'success': success, 'data': [{'details': events}]}
    cases = []
    def add(name, provider, payload, status=200):
        cases.append(dict(name=name, provider=provider, http_status=status, body=json.dumps(payload, ensure_ascii=False)))
    for provider in ('rtb56', 'txfba'):
        add(provider + '-success', provider, rtb([event]))
    add('multiple-order', 'rtb56', rtb([dict(event, track_description='较晚', track_occur_date='2026-01-03 10:00:00'), event]))
    add('duplicate-events', 'rtb56', rtb([event, event]))
    add('long-description', 'rtb56', rtb([dict(event, track_description='虚构长描述' * 300)]))
    add('missing-time', 'rtb56', rtb([{'track_description': '虚构无时间'}]))
    add('empty', 'rtb56', rtb([]))
    for zero in ('0', 0):
        add('business-zero-' + type(zero).__name__, 'rtb56', rtb([], zero))
    add('http-error', 'rtb56', rtb([event]), 503)
    cases.append(dict(name='invalid-json', provider='rtb56', http_status=200, body='fictional non-JSON'))
    add('missing-description', 'rtb56', rtb([{'track_location': '虚构'}]))
    add('nextsls-success', 'nextsls', {'status': 1, 'data': {'shipment': {'traces': [{'time': '2026-01-02T10:00:00+08:00', 'location': '虚构', 'info': '虚构NextSLS'}]}}})
    add('nextsls-epoch', 'nextsls', {'status': 1, 'data': {'shipment': {'traces': [{'time': 0, 'info': '虚构epoch'}]}}})
    add('17track-success', '17track', {'status': 200, 'data': [{'track': [{'time': '2026-01-02', 'location': '虚构', 'description': '虚构第三方'}]}]})
    add('generic-unverified', 'generic', {'tracks': [{'time': None, 'description': '虚构已签收文本', 'status': 'delivered'}]})
    add('generic-explicit-error', 'generic', {'code': 500, 'tracks': [{'description': '虚构'}]})
    return cases
