import unittest
from unittest.mock import patch
import test_v1 as base
from models import db, Shipment, Customer, CarrierAgent

class BusinessFinal(unittest.TestCase):
    setUpClass=classmethod(base.StabilityTests.setUpClass.__func__)
    setUp=base.StabilityTests.setUp
    tearDown=base.StabilityTests.tearDown
    token=base.StabilityTests.token
    post=base.StabilityTests.post
    login=base.StabilityTests.login
    def test_add_validation_no_partial_records(self):
        self.login()
        for fields in ({},{'tracking_number':'NEW','weight':'nan'}, {'tracking_number':'NEW','pieces':'x'},
                       {'tracking_number':'NEW','customer_id':'999'}, {'tracking_number':'NEW','unit_price':'-1'}):
            self.assertEqual(self.post('/shipments/add',data=fields).status_code,400)
            self.assertEqual(Shipment.query.count(),1)
    def test_add_offline_no_remote_and_fee(self):
        self.login()
        with patch('carrier_http.post') as remote:
            r=self.post('/shipments/add',data={'tracking_number':'NEW','weight':'2','unit_price':'10','operation_fee':'3'})
        self.assertEqual(r.status_code,302);remote.assert_not_called()
        self.assertEqual(Shipment.query.filter_by(tracking_number='NEW').one().fee,23)
    def test_duplicate_customer_and_agent(self):
        self.login()
        self.post('/customers',data={'name':self.customer.name})
        self.post('/agents',data={'name':self.agent.name})
        self.assertEqual(Customer.query.count(),1);self.assertEqual(CarrierAgent.query.count(),1)
    def test_agent_rename_collision_keeps_association(self):
        self.login();other=CarrierAgent(name='OTHER');db.session.add(other);db.session.commit()
        self.assertEqual(self.post(f'/agents/{self.agent.id}/edit',data={'name':'OTHER'}).status_code,400)
        self.assertEqual(self.shipment.agent_id,self.agent.id)
    def test_provider_number_search(self):
        self.login()
        r=self.client.get('/shipments',query_string={'tracking_number':self.shipment.shipment_id})
        self.assertIn('FICTIONAL-001',r.text)
    def test_reset_guard_precedes_import(self):
        import ast
        from pathlib import Path
        tree=ast.parse(Path('db_reset.py').read_text())
        self.assertIsInstance(tree.body[0],ast.Raise)
    def test_manual_validation_and_no_offline_sync(self):
        from models import ManualTrack
        self.login()
        self.assertEqual(self.post('/shipments/1/tracks',data={'description':' '}).status_code,400)
        self.assertEqual(ManualTrack.query.count(),0)
        with patch('views.requests.patch') as remote:
            self.assertEqual(self.post('/shipments/1/tracks',data={'description':'虚构入库'}).status_code,302)
        remote.assert_not_called()
        self.assertEqual(ManualTrack.query.count(),1)
    def test_tracking_visible_without_javascript(self):
        self.login()
        r=self.post('/track',data={'numbers':'FICTIONAL-001'})
        self.assertIn('<details open',r.text)
        self.assertNotIn('data-bs-toggle',r.text)
    def test_invoice_formula_is_literal(self):
        from utils import export_invoice
        from openpyxl import load_workbook
        output=export_invoice({},'虚构客户',[{'order_no':'=1+1','summary':'=HYPERLINK("https://fictional.invalid")','fee':1}],[],filename=None)
        book=load_workbook(output)
        self.assertFalse(any(c.data_type=='f' for row in book.active for c in row))
    def test_bulk_query_limit_before_provider(self):
        self.login()
        with patch('views.fetch_tracking_from_api') as remote:
            r=self.post('/track',data={'numbers':'\n'.join('FAKE-'+str(i) for i in range(201))})
        self.assertEqual(r.status_code,400);remote.assert_not_called()

    def test_filtered_pages_use_stable_id_tiebreak_and_search_all_rows(self):
        import re
        self.login()
        db.session.add_all([
            Shipment(tracking_number=f'PAGE-{i:03}', shipment_id=f'PROVIDER-PAGE-{i:03}',
                     created_at=self.shipment.created_at, customer_id=self.customer.id, agent_id=self.agent.id)
            for i in range(55)
        ])
        db.session.commit()
        filters = {'customer': self.customer.name, 'agent': self.agent.name, 'tracking_number': 'PAGE-'}
        first = self.client.get('/shipments', query_string=filters)
        link = re.search(r'rel="next" href="([^"]+)"', first.text).group(1).replace('&amp;', '&')
        second = self.client.get(link)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn('共 55 票', second.text)
        self.assertNotIn('PROVIDER-PAGE-004', first.text)
        self.assertIn('PROVIDER-PAGE-004', second.text)
        self.assertNotIn('PROVIDER-PAGE-005', second.text)
        self.assertIn('rel="prev"', second.text)
        search = self.client.get('/shipments', query_string={'tracking_number': 'PROVIDER-PAGE-000'})
        self.assertIn('PROVIDER-PAGE-000', search.text)

    def test_batch_reads_fixed_sql_budget_and_isolates_failure(self):
        from sqlalchemy import event
        self.login()
        db.session.add_all([Shipment(tracking_number=f'BATCH-{i:03}', agent_id=self.agent.id)
                            for i in range(25)])
        db.session.commit()
        queries = []
        def count_sql(conn, cursor, statement, parameters, context, executemany):
            queries.append(statement)
        engine = db.engine
        token = self.token()
        numbers = [f'BATCH-{i:03}' for i in reversed(range(25))]
        def fetch(agent, number, **kwargs):
            if number == numbers[0]:
                raise RuntimeError('unexpected supplier failure')
            events = [{'description': '虚构成功轨迹'}]
            if number == numbers[1]:
                events.insert(0, None)
            return events, None
        event.listen(engine, 'before_cursor_execute', count_sql)
        try:
            with patch('views.fetch_tracking_from_api', side_effect=fetch) as remote:
                response = self.client.post('/track', data={'numbers': '\n'.join(numbers + [numbers[0]])},
                                            headers={'X-CSRFToken': token})
        finally:
            event.remove(engine, 'before_cursor_execute', count_sql)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 7)
        self.assertEqual([call.args[1] for call in remote.call_args_list], numbers)
        self.assertIn('该票物流查询失败', response.text)
        self.assertIn('该票部分物流轨迹格式异常', response.text)
        self.assertEqual(response.text.count('虚构成功轨迹'), 24)

    def test_legacy_invoice_layout_after_consolidation(self):
        from app import generate_invoice_xlsx
        from openpyxl import load_workbook
        rows = [['2026-01-02', 'FAKE-ONE', 'PROVIDER-ONE', 1, 2, '虚构目的地', '测试', 12.5, '摘要'],
                ['2026-01-03', 'FAKE-TWO', 'PROVIDER-TWO', 2, 3, '虚构目的地', '测试', 7.5, '摘要二']]
        book = load_workbook(generate_invoice_xlsx('虚构公司', '虚构客户', '虚构银行', rows))
        sheet = book.active
        self.assertEqual(sheet.title, '账单')
        self.assertEqual(sheet['B3'].value, '虚构客户')
        self.assertEqual(sheet['C5'].value, 'FAKE-ONE')
        self.assertEqual(sheet['C6'].value, 'FAKE-TWO')
        self.assertEqual(sheet['I8'].value, 20)
        self.assertEqual(sheet['A11'].value, '虚构银行')

    def test_legacy_endpoint_aliases_preserve_urls_and_matching(self):
        from flask import url_for
        app = self.app
        cases = [('logout', {}, '/logout', 'POST'), ('agents', {}, '/agents', 'GET'),
                 ('edit_agent', {'agent_id': 1}, '/agents/1/edit', 'GET'),
                 ('delete_agent', {'agent_id': 1}, '/agents/1/delete', 'POST'),
                 ('shipments', {}, '/shipments', 'GET'), ('add_shipment', {}, '/shipments/add', 'GET'),
                 ('customers', {}, '/customers', 'GET'), ('finance', {}, '/finance', 'GET'),
                 ('bank_accounts', {}, '/bank_accounts', 'GET'),
                 ('manual_tracks', {'sid': 1}, '/shipments/1/tracks', 'GET'),
                 ('track', {}, '/track', 'POST')]
        with app.test_request_context():
            for endpoint, values, path, method in cases:
                self.assertEqual(url_for(endpoint, **values), path)
                matched, _ = app.url_map.bind('localhost').match(path, method=method)
                self.assertEqual(matched, 'views.' + endpoint)

    def test_old_startup_blocks_before_import_or_database_access(self):
        import ast
        from pathlib import Path
        tree = ast.parse(Path('init_db.py').read_text())
        self.assertIsInstance(tree.body[0], ast.Raise)
        script = Path('run.sh').read_text()
        self.assertLess(script.index('exit 1'), script.index('python init_db.py'))
