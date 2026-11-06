"""Offline SSRF, credential and /track regressions; all credentials are fictional."""
import contextlib
import io
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from flask import Flask
import carrier_http as transport


def addresses(*ips):
    return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM,
             6, '', (ip, 443)) for ip in ips]


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self.env = patch.dict('os.environ', {'LOGISTICS_ISOLATED': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.agent = SimpleNamespace(is_active=True, supports_api=True,
            api_url='https://carrier.example/track', app_key='fictional-key',
            app_token='fictional-token', password=None)
        self.dns = patch('carrier_http.socket.getaddrinfo', return_value=addresses('8.8.8.8')).start()
        self.pool_type = patch('carrier_http.urllib3.HTTPSConnectionPool').start()
        self.http_pool_type = patch('carrier_http.urllib3.HTTPConnectionPool').start()
        self.http_pool_type.return_value = self.pool_type.return_value
        self.addCleanup(patch.stopall)
        self.pool = self.pool_type.return_value
        self.response = self.pool.urlopen.return_value
        self.response.status = 200
        self.response.read.return_value = b'{"tracks":[]}'

    def send(self):
        return transport.post(self.agent, data={'appKey':self.agent.app_key, 'appToken':self.agent.app_token})

    def test_pins_validated_ip_preserves_tls_name_and_disables_redirect_retry(self):
        self.assertEqual(self.send().json(), {'tracks': []})
        self.dns.assert_called_once_with('carrier.example',443,type=socket.SOCK_STREAM)
        args, kw = self.pool_type.call_args
        self.assertEqual(args, ('8.8.8.8',))
        self.assertEqual(kw['server_hostname'], 'carrier.example')
        self.assertEqual(kw['assert_hostname'], 'carrier.example')
        self.assertEqual(kw['cert_reqs'], 'CERT_REQUIRED')
        args, kw = self.pool.urlopen.call_args
        self.assertEqual(args, ('POST', '/track'))
        self.assertEqual(kw['headers']['Host'],'carrier.example')
        self.assertFalse(kw['redirect']); self.assertFalse(kw['retries'])
        self.assertIn('fictional-key', kw['body'])
        self.response.close.assert_called_once(); self.pool.close.assert_called_once()

    def test_existing_http_form_url_query_and_auth_unchanged(self):
        self.agent.api_url = 'http://ywsl.rtb56.com/webservice/PublicService.asmx/ServiceInterfaceUTF8?version=1'
        payload = {'appKey': self.agent.app_key, 'appToken': self.agent.app_token,
                   'serviceMethod': 'gettrack', 'paramsJson': '{"tracking_number":"FAKE-001"}'}
        result = transport.post(self.agent, data=payload,
                                headers={'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(result.json(), {'tracks': []})
        self.dns.assert_called_once_with('ywsl.rtb56.com', 80, type=socket.SOCK_STREAM)
        self.http_pool_type.assert_called_once_with('8.8.8.8', port=80, retries=False)
        self.pool_type.assert_not_called()
        args, kw = self.pool.urlopen.call_args
        self.assertEqual(args, ('POST', '/webservice/PublicService.asmx/ServiceInterfaceUTF8?version=1'))
        self.assertEqual(kw['headers']['Host'], 'ywsl.rtb56.com')
        from urllib.parse import parse_qs
        self.assertEqual(parse_qs(kw['body']), {k:[v] for k,v in payload.items()})
        self.assertFalse(kw['redirect']); self.assertFalse(kw['retries'])

    def test_dynamic_hosts_and_standard_ports(self):
        for url in ('http://new-carrier.example/api', 'https://another.example/api',
                    'http://8.8.8.8/api', 'https://carrier.example:443/api'):
            self.agent.api_url = url
            self.assertEqual(self.send().status_code, 200)

    def test_nonstandard_ports_rejected(self):
        for url in ('http://carrier.example:8080/api', 'http://carrier.example:443/api',
                    'https://carrier.example:80/api', 'https://carrier.example:0/api'):
            self.agent.api_url = url
            with self.assertRaises(transport.CarrierRequestError):
                self.send()
        self.dns.assert_not_called()

    def test_socket_connect_uses_validated_numeric_ip_without_dns(self):
        connection = SimpleNamespace(host='8.8.8.8', port=443, timeout=5)
        with patch('carrier_http.socket.socket') as socket_type:
            sock = socket_type.return_value
            sock.getpeername.return_value = ('8.8.8.8', 443)
            self.assertIs(transport._connect_pinned(connection), sock)
            sock.connect.assert_called_once_with(('8.8.8.8', 443))
        self.dns.assert_not_called()

    def test_changed_dns_cannot_change_connection_target(self):
        self.dns.side_effect = [addresses('8.8.8.8'), addresses('127.0.0.1')]
        self.send()
        self.assertEqual(self.dns.call_count, 1)
        self.assertEqual(self.pool_type.call_args.args, ('8.8.8.8',))
        self.assertIs(self.pool.ConnectionCls, transport.PinnedHTTPSConnection)

    def test_authorized_http_still_blocks_private_mixed_dns_and_redirects(self):
        self.agent.api_url = 'http://ywsl.rtb56.com/api'
        for ips in (('127.0.0.1',), ('169.254.169.254',), ('168.63.129.16',),
                    ('8.8.8.8','10.0.0.1')):
            self.dns.return_value=addresses(*ips)
            with self.assertRaises(transport.CarrierRequestError): self.send()
        self.http_pool_type.assert_not_called()
        self.dns.return_value=addresses('8.8.8.8')
        for status in (301,302,303,307,308):
            self.response.status=status
            with self.assertRaises(transport.CarrierRequestError): self.send()
        self.response.read.assert_not_called()
        self.assertEqual(self.pool.urlopen.call_count,5)
        for call in self.pool.urlopen.call_args_list:
            self.assertFalse(call.kwargs['redirect'])

    def test_environment_proxy_cannot_override_http_destination(self):
        self.agent.api_url='http://ywsl.rtb56.com/api'
        with patch.dict('os.environ',HTTP_PROXY='http://127.0.0.1:1',
                        HTTPS_PROXY='http://127.0.0.1:1',ALL_PROXY='http://127.0.0.1:1'):
            self.send()
        self.http_pool_type.assert_called_once_with('8.8.8.8',port=80,retries=False)

    def test_private_metadata_and_special_addresses_rejected(self):
        for ip in ('127.0.0.1','10.0.0.1','172.16.0.1','192.168.1.1','169.254.169.254',
                   '100.100.100.200','0.0.0.0','224.0.0.1','168.63.129.16',
                   '::1','fc00::1','fe80::1','::ffff:8.8.8.8','2002:0808:0808::1'):
            with self.subTest(ip=ip):
                self.dns.return_value=addresses(ip)
                with self.assertRaises(transport.CarrierRequestError): self.send()
        self.pool_type.assert_not_called(); self.http_pool_type.assert_not_called()

    def test_mixed_dns_answers_fail_closed(self):
        self.dns.return_value=addresses('8.8.8.8','127.0.0.1')
        with self.assertRaises(transport.CarrierRequestError): self.send()
        self.pool_type.assert_not_called(); self.http_pool_type.assert_not_called()

    def test_bad_urls_rejected_before_dns(self):
        for url in ('http://localhost/api','file:///tmp/a','ftp://carrier.example',
                    'https://user:secret@carrier.example','https://carrier.example:8443',
                    'https://carrier.example/#secret','https://carrier.example\\@127.0.0.1',
                    'https://carrier.example/\npath','https://[fe80::1%25eth0]/'):
            with self.subTest(url=url):
                self.agent.api_url=url
                with self.assertRaises(transport.CarrierRequestError): self.send()
        self.dns.assert_not_called(); self.pool_type.assert_not_called(); self.http_pool_type.assert_not_called()

    def test_dns_failure_is_sanitized(self):
        self.dns.side_effect=RuntimeError('fictional-token')
        with self.assertRaises(transport.CarrierRequestError) as error: self.send()
        self.assertNotIn('fictional-token',str(error.exception))

    def test_redirects_never_followed_or_read(self):
        for status in (301,302,303,307,308):
            self.response.status=status
            with self.assertRaises(transport.CarrierRequestError): self.send()
        self.response.read.assert_not_called()
        self.assertEqual(self.pool.urlopen.call_count,5)

    def test_errors_and_non_json_do_not_echo_secrets(self):
        for status, body in ((403,b'fictional-key'),(200,b'fictional-token')):
            self.response.status=status; self.response.read.return_value=body
            with self.assertRaises(transport.CarrierRequestError) as error: self.send()
            self.assertNotIn('fictional-',str(error.exception))
        self.pool.urlopen.side_effect=RuntimeError('fictional-token')
        with self.assertRaises(transport.CarrierRequestError) as error: self.send()
        self.assertNotIn('fictional-',str(error.exception))

    def test_echoed_json_credentials_redacted(self):
        self.response.read.return_value=json.dumps({'message':'fictional-key fictional-token',
              'tracks':[{'description':'fictional-token'}], 'fictional-key':'value'}).encode()
        result=self.send()
        self.assertNotIn('fictional-', result.text)
        self.assertIn('[REDACTED]',result.text)

    def test_large_response_rejected(self):
        self.response.read.return_value=b'x'*(2*1024*1024+1)
        with self.assertRaises(transport.CarrierRequestError): self.send()

    def test_inactive_unconfigured_isolated_and_no_context_block_before_dns(self):
        self.agent.is_active=False
        with self.assertRaises(transport.CarrierRequestError): self.send()
        self.agent.is_active=True; self.agent.supports_api=False
        with self.assertRaises(transport.CarrierRequestError): self.send()
        self.agent.supports_api=True; self.app.config['ISOLATED_MODE']=True
        with self.assertRaises(transport.CarrierRequestError): self.send()
        self.app.config['ISOLATED_MODE']=False
        with patch.dict('os.environ', LOGISTICS_ISOLATED='1'):
            with self.assertRaises(transport.CarrierRequestError): self.send()
        self.ctx.pop()
        try:
            with self.assertRaises(transport.CarrierRequestError): self.send()
        finally:
            self.ctx.push()
        self.dns.assert_not_called()


import test_v1 as baseline
from models import db


class CredentialAndRouteTests(unittest.TestCase):
    setUpClass=classmethod(baseline.StabilityTests.setUpClass.__func__)
    tearDown=baseline.StabilityTests.tearDown
    token=baseline.StabilityTests.token
    post=baseline.StabilityTests.post
    login=baseline.StabilityTests.login

    def setUp(self):
        baseline.StabilityTests.setUp(self)
        self.login()
        self.agent.supports_api=True
        self.agent.api_url='https://carrier.example/track'
        self.agent.app_key='fictional-key';self.agent.app_token='fictional-token'
        db.session.commit()

    def test_empty_missing_whitespace_preserve_and_replacement_updates(self):
        path=f'/agents/{self.agent.id}/edit'
        for fields in ({},{'app_key':'','app_token':''},{'app_key':'  ','app_token':'\t'}):
            self.assertEqual(self.post(path,data=fields).status_code,302)
            self.assertEqual((self.agent.app_key,self.agent.app_token),('fictional-key','fictional-token'))
        html=self.client.get(path).get_data(as_text=True)
        self.assertNotIn('fictional-key',html); self.assertNotIn('fictional-token',html)
        self.post(path,data={'app_key':'new-fictional-key','app_token':'new-fictional-token'})
        self.assertEqual((self.agent.app_key,self.agent.app_token),('new-fictional-key','new-fictional-token'))

    def test_track_reaches_secure_transport_and_redacts_upstream_echo(self):
        output=io.StringIO()
        with patch.dict('os.environ',LOGISTICS_ISOLATED='0'), \
             patch.dict(self.app.config,ISOLATED_MODE=False), \
             patch('carrier_http.socket.getaddrinfo',return_value=addresses('8.8.8.8')), \
             patch('carrier_http.urllib3.HTTPSConnectionPool') as pool, \
             contextlib.redirect_stdout(output):
            response=pool.return_value.urlopen.return_value
            response.status=200
            response.read.return_value=b'{"tracks":[{"description":"arrived fictional-token"}]}'
            result=self.post('/track',data={'numbers':self.shipment.tracking_number})
            self.assertEqual(pool.return_value.urlopen.call_count,1)
            body=pool.return_value.urlopen.call_args.kwargs['body']
            self.assertIn('appKey=fictional-key',body);self.assertIn('appToken=fictional-token',body)
        html=result.get_data(as_text=True)
        self.assertIn('arrived',html)
        self.assertNotIn('fictional-token',html+output.getvalue())
        self.assertNotIn('fictional-key',html+output.getvalue())

    def test_http_track_preserves_original_txfba_parameters_and_hidden_credentials(self):
        from urllib.parse import parse_qs
        self.agent.api_url='http://fictional.txfba.com/api?version=1'
        db.session.commit()
        original_url=self.agent.api_url
        output=io.StringIO()
        with patch.dict('os.environ',LOGISTICS_ISOLATED='0'), \
             patch.dict(self.app.config,ISOLATED_MODE=False), \
             patch('carrier_http.socket.getaddrinfo',return_value=addresses('8.8.8.8')), \
             patch('carrier_http.urllib3.HTTPConnectionPool') as pool, \
             contextlib.redirect_stdout(output):
            response=pool.return_value.urlopen.return_value
            response.status=200
            response.read.return_value=json.dumps({'success':1,'data':[{'details':[
                {'track_description':'arrived fictional-token','track_location':'FAKE'}]}]}).encode()
            result=self.post('/track',data={'numbers':self.shipment.tracking_number})
            self.assertEqual(pool.return_value.urlopen.call_count,1)
            args, kw=pool.return_value.urlopen.call_args
            self.assertEqual(args,('POST','/api?version=1'))
            body=parse_qs(kw['body'])
            self.assertEqual(body['appKey'],['fictional-key'])
            self.assertEqual(body['appToken'],['fictional-token'])
            self.assertEqual(body['serviceMethod'],['gettrack'])
            self.assertEqual(json.loads(body['paramsJson'][0]),{'tracking_number':self.shipment.tracking_number})
            self.assertFalse(kw['redirect'])
        self.assertEqual(self.agent.api_url,original_url)
        html=result.get_data(as_text=True)
        self.assertIn('arrived',html)
        self.assertNotIn('fictional-token',html+output.getvalue())
        self.assertNotIn('fictional-key',html+output.getvalue())

    def test_flask_admin_never_renders_saved_credentials(self):
        for path in ('/admin/carrieragent/', f'/admin/carrieragent/edit/?id={self.agent.id}'):
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200)
            self.assertNotIn('fictional-key', result.text)
            self.assertNotIn('fictional-token', result.text)

    def test_create_new_carrier_persists_credentials_without_display(self):
        from models import CarrierAgent
        result = self.post('/agents', data={'name': 'new-fixture-carrier',
            'api_url': 'https://new-fixture.example/track',
            'app_key': 'fixture-new-key', 'app_token': 'fixture-new-token'})
        self.assertEqual(result.status_code, 302)
        agent = CarrierAgent.query.filter_by(name='new-fixture-carrier').one()
        self.assertTrue(agent.is_active)
        self.assertTrue(agent.supports_api)
        self.assertEqual(agent.api_url, 'https://new-fixture.example/track')
        self.assertEqual(agent.app_key, 'fixture-new-key')
        self.assertEqual(agent.app_token, 'fixture-new-token')
        result = self.client.get(f'/agents/{agent.id}/edit')
        self.assertNotIn('fixture-new-key', result.text)
        self.assertNotIn('fixture-new-token', result.text)
