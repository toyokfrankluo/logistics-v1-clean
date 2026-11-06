"""Only configured carrier queries may cross the release's HTTP deny guard.

Database-configured HTTP/80 and HTTPS/443 endpoints; resolve once, reject every non-public
answer, and pin the connection to that IP. No redirects or environment proxies.
"""
import ipaddress
import json as jsonlib
import os
import socket
from urllib.parse import urlsplit, quote

import certifi
import requests
import urllib3
from flask import current_app, has_app_context


class CarrierRequestError(RuntimeError):
    pass


def _endpoint(url):
    try:
        if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url) or '\\' in url:
            raise ValueError()
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise ValueError()
        host = parsed.hostname.encode('idna').decode('ascii')
        port = parsed.port if parsed.port is not None else (80 if parsed.scheme == 'http' else 443)
        if '%' in host or port != (80 if parsed.scheme == 'http' else 443) or host.rstrip('.').lower() == 'localhost' or host.rstrip('.').lower().endswith('.localhost'):
            raise ValueError()
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(row[4][0]) for row in addresses]
        if not ips or any(str(ip) == "168.63.129.16" or not ip.is_global or ip.is_multicast or ip.is_reserved
                          or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))
                          for ip in ips):
            raise ValueError()
        return parsed.scheme, host, port, str(ips[0])
    except Exception:
        raise CarrierRequestError('代理 API 地址未通过安全检查（仅允许公网 HTTP/80 或 HTTPS/443）') from None


def _connect_pinned(connection):
    """Connect a numeric address directly; never resolve a hostname here."""
    ip = ipaddress.ip_address(connection.host)
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(connection.timeout)
        sock.connect((str(ip), connection.port))
        if ipaddress.ip_address(sock.getpeername()[0]) != ip:
            raise CarrierRequestError("代理 API 连接目标不一致")
        return sock
    except BaseException:
        sock.close()
        raise


class PinnedHTTPConnection(urllib3.connection.HTTPConnection):
    def _new_conn(self):
        return _connect_pinned(self)


class PinnedHTTPSConnection(urllib3.connection.HTTPSConnection):
    def _new_conn(self):
        return _connect_pinned(self)


def _redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            for form in (secret, quote(secret, safe=''), quote(secret, safe='').replace('%20', '+')):
                value = value.replace(form, '[REDACTED]')
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {_redact(key, secrets): _redact(item, secrets) for key, item in value.items()}
    return value


def post(agent, *, data=None, json=None, headers=None, timeout=15):
    # No saved requests method is restored; isolated runs remain fully offline.
    if (os.getenv('LOGISTICS_ISOLATED') == '1' or not has_app_context()
            or current_app.config.get('ISOLATED_MODE')):
        raise CarrierRequestError('隔离模式禁止外部物流请求')
    if not agent or not agent.is_active or not agent.supports_api or not agent.api_url:
        raise CarrierRequestError('代理未启用或未配置 API')
    scheme, host, port, ip = _endpoint(agent.api_url)
    secrets = [getattr(agent, field, None) for field in ('app_key', 'app_token', 'password')]
    secrets = [value for value in secrets if isinstance(value, str) and value]
    pool = response = None
    try:
        # Preparing a request performs encoding only, without Session/netrc/env access.
        prepared = requests.Request('POST', agent.api_url, data=data, json=json, headers=headers).prepare()
        request_headers = dict(prepared.headers)
        request_headers['Host'] = '[' + host + ']' if ':' in host else host
        if port != (80 if scheme == 'http' else 443):
            request_headers['Host'] += ':' + str(port)
        if scheme == 'http':
            pool = urllib3.HTTPConnectionPool(ip, port=port, retries=False)
            pool.ConnectionCls = PinnedHTTPConnection
        else:
            pool = urllib3.HTTPSConnectionPool(ip, port=port, server_hostname=host,
                                             assert_hostname=host, cert_reqs='CERT_REQUIRED',
                                             ca_certs=certifi.where(), retries=False)
            pool.ConnectionCls = PinnedHTTPSConnection
        response = pool.urlopen('POST', prepared.path_url, body=prepared.body,
                                headers=request_headers, redirect=False, retries=False,
                                timeout=urllib3.Timeout(connect=min(timeout, 15), read=min(timeout, 15)),
                                preload_content=False)
        if 300 <= response.status < 400:
            raise CarrierRequestError('代理 API 重定向已阻止')
        if response.status != 200:
            raise CarrierRequestError('代理 API 返回非成功状态')
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise CarrierRequestError('代理 API 响应过大')
        payload = _redact(jsonlib.loads(raw), secrets)
        result = requests.Response()
        result.status_code = response.status
        result.encoding = 'utf-8'
        result._content = jsonlib.dumps(payload, ensure_ascii=False).encode('utf-8')
        return result
    except CarrierRequestError:
        raise
    except Exception:
        # Never return transport exceptions, URLs, headers or raw upstream bodies.
        raise CarrierRequestError('代理 API 请求失败，请检查配置或稍后重试') from None
    finally:
        if response is not None:
            response.close()
        if pool is not None:
            pool.close()
