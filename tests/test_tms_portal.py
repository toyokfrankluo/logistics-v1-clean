"""Phase 2 regression checks. All accounts, addresses and workbooks are synthetic."""
import io
import os
from pathlib import Path
import re
import secrets
import tempfile
import unittest
from unittest.mock import patch

os.environ["LOGISTICS_ISOLATED"] = "1"

from werkzeug.security import check_password_hash, generate_password_hash
from app import app, User
from models import CarrierAgent, Customer, Shipment, db
from tms_invoice import InvoiceError, SERVICES, blank_invoice, parse_invoice
from tms_models import TmsAccount, TmsAudit, TmsOrder


def workbook_bytes(order_number="0000123", code="DEMO-A", *, change=None, recipient="虚构地址甲"):
    book = blank_invoice()
    address_sheet = book["地址库"]
    address_sheet.append(("虚构仓库", code, "", "", recipient, "", "", "", "1 Fictional Way",
                          "", "", "Test City", "CA", "US", "00001"))
    sheet = book["星睿发票"]
    for cell, value in {
        "D4": order_number, "D5": "UPS红单", "L4": code, "D7": "否",
        "D8": "否", "D9": "否", "D10": "否", "D15": 2,
        "A23": "0001", "B23": "Demo item", "C23": "虚构商品", "G23": 1.25,
        "H23": 2, "J23": 1.5, "K23": 20, "L23": 10, "M23": 8, "N23": "否",
        "A24": "0001", "B24": "Second item", "C24": "虚构明细", "G24": 3,
        "H24": 1, "N24": "否",
        "A25": "0002", "B25": "Third item", "C25": "虚构另一箱", "G25": 2,
        "H25": 1, "J25": 1, "K25": 10, "L25": 10, "M25": 10, "N25": "否",
    }.items():
        sheet[cell] = value
    if change:
        change(book)
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


class InvoiceParsingTests(unittest.TestCase):
    def test_group_boxes_and_compute_without_formula_cache(self):
        raw = workbook_bytes(recipient="上传文件中伪造的地址")
        invoice = parse_invoice(raw, workbook_bytes())
        self.assertEqual(invoice["customer_number"], "0000123")
        self.assertEqual(invoice["address"]["recipient"], "虚构地址甲")
        self.assertEqual(invoice["address"]["postal_code"], "00001")
        self.assertEqual([item["box_number"] for item in invoice["boxes"]], ["0001", "0002"])
        self.assertEqual(len(invoice["declarations"]), 3)
        self.assertEqual(invoice["declarations"][0]["total_usd"], "2.50")
        self.assertEqual(len(SERVICES), 13)

    def test_unknown_catalogue_and_duplicate_address_code(self):
        with self.assertRaisesRegex(InvoiceError, "不存在或重复"):
            parse_invoice(workbook_bytes(), workbook_bytes(code="DEMO-B"))
        def duplicate(book):
            sheet = book["地址库"]
            sheet.append([cell.value for cell in sheet[2]])
        with self.assertRaisesRegex(InvoiceError, "不存在或重复"):
            parse_invoice(workbook_bytes(), workbook_bytes(change=duplicate))

    def test_bad_types_and_format(self):
        for cell, value in (("D4", 123), ("D5", "无效服务"), ("G23", "=1+1"),
                            ("N23", "也许"), ("D15", 3), ("J23", -1)):
            with self.subTest(cell=cell), self.assertRaises(InvoiceError):
                raw = workbook_bytes(change=lambda book: book["星睿发票"].__setitem__(cell, value))
                parse_invoice(raw, workbook_bytes())
        with self.assertRaises(InvoiceError):
            parse_invoice(b"not an xlsx", workbook_bytes())

    def test_active_formula_and_conflicting_box_measurements_rejected(self):
        def formula(book):
            book["地址库"]["P2"] = '=WEBSERVICE("https://example.invalid")'
        def conflict(book):
            for cell, value in {"J24": 4, "K24": 20, "L24": 10, "M24": 8}.items():
                book["星睿发票"][cell] = value
        with self.assertRaisesRegex(InvoiceError, "公式函数"):
            parse_invoice(workbook_bytes(change=formula), workbook_bytes())
        with self.assertRaisesRegex(InvoiceError, "重复箱号"):
            parse_invoice(workbook_bytes(change=conflict), workbook_bytes())


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.assertTrue(app.config["ISOLATED_MODE"])
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(app.config, TESTING=True, TMS_ENABLED=True,
                                     TMS_ADDRESS_BOOK_DIR=directory))
        self.carrier_post = self.enterContext(patch("carrier_http.post", side_effect=AssertionError))
        db.create_all()
        self.addCleanup(self.drop_tables)
        self.password = secrets.token_urlsafe(22)
        self.customer_a = Customer(name="虚构甲")
        self.customer_b = Customer(name="虚构乙")
        self.agent = CarrierAgent(name="虚构代理", is_active=True)
        db.session.add_all([self.customer_a, self.customer_b, self.agent])
        db.session.flush()
        for customer, username, code in ((self.customer_a, "customer-a", "DEMO-A"),
                                         (self.customer_b, "customer-b", "DEMO-B")):
            Path(directory, f"{customer.id}.xlsx").write_bytes(workbook_bytes(code=code))
            db.session.add(TmsAccount(username=username, customer_id=customer.id,
                                      password_hash=generate_password_hash(self.password)))
        for username, admin in (("tms-test-staff", True), ("tms-test-reader", False)):
            staff = User(username=username, is_admin=admin)
            staff.set_password(self.password)
            db.session.add(staff)
        db.session.commit()
        self.alice = app.test_client()
        self.bob = app.test_client()
        self.staff = app.test_client()

    def drop_tables(self):
        self.carrier_post.assert_not_called()
        db.session.remove()
        db.drop_all()

    def post(self, client, path, data, login_page="/tms/customer/login"):
        with app.app_context():
            html = client.get(login_page).get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        with app.app_context():
            return client.post(path, data=data, headers={"X-CSRFToken": token})

    def get(self, client, path):
        with app.app_context():
            return client.get(path)

    def sign_in(self, client, username):
        return self.post(client, "/tms/customer/login", {"username": username, "password": self.password})

    def staff_login(self, username="tms-test-staff"):
        return self.post(self.staff, "/login", {"username": username, "password": self.password}, "/login")

    def upload(self, client, raw=None, **form):
        return self.post(client, "/tms/customer/import",
                         {"invoice": (io.BytesIO(raw or workbook_bytes()), "fictional.xlsx"), **form})

    def decision(self, order_id, revision, **form):
        return self.post(self.staff, f"/tms/admin/orders/{order_id}/decision",
                         {"revision": revision, **form}, "/login")

    def get_order(self, client=None):
        if client is None:
            client = self.alice
        self.assertEqual(self.sign_in(client, "customer-a").status_code, 302)
        response = self.upload(client)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return int(response.location.rsplit("/", 1)[1])

    def test_customer_isolation_list_detail_download_and_staff_boundary(self):
        order_id = self.get_order()
        self.assertEqual(self.sign_in(self.bob, "customer-b").status_code, 302)
        self.assertIn("当前没有符合条件的订单", self.get(self.bob, "/tms/customer/?q=0000123").get_data(as_text=True))
        for path in (f"/tms/customer/orders/{order_id}", f"/tms/customer/orders/{order_id}/invoice",
                     f"/tms/customer/import?replace={order_id}"):
            self.assertEqual(self.get(self.bob, path).status_code, 404)
        self.assertEqual(self.get(self.bob, "/tms/admin/").status_code, 302)
        self.assertEqual(self.get(self.bob, "/shipments").status_code, 302)
        own = self.get(self.alice, f"/tms/customer/orders/{order_id}/invoice")
        self.assertEqual(own.status_code, 200)
        self.assertIn("attachment", own.headers["Content-Disposition"])
        self.assertIn("no-store", own.headers["Cache-Control"])

    def test_auth_review_end_to_end(self):
        order_id = self.get_order()
        preview = self.get(self.alice, f"/tms/customer/orders/{order_id}")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("导入预览", preview.get_data(as_text=True))
        self.staff_login()
        self.assertEqual(self.get(self.staff, f"/tms/admin/orders/{order_id}").status_code, 404)
        path = f"/tms/customer/orders/{order_id}/confirm"
        self.assertEqual(self.post(self.alice, path, {"revision": 1}).status_code, 302)
        self.assertEqual(self.post(self.alice, path, {"revision": 1}).status_code, 409)
        self.assertIn(b"0000123", self.get(self.staff, "/tms/admin/").data)
        self.assertEqual(self.decision(order_id, 2, decision="approve", agent_id=self.agent.id,
                                       agent_service="内部代理服务代码").status_code, 302)
        self.assertEqual(self.decision(order_id, 2, decision="reject", reason="重复").status_code, 409)
        db.session.expire_all()
        self.assertEqual(db.session.get(TmsOrder, order_id).status, "approved")
        self.assertEqual(TmsAudit.query.count(), 3)
        self.assertNotIn("内部代理服务代码", self.get(self.alice, f"/tms/customer/orders/{order_id}").get_data(as_text=True))
        self.assertEqual(Shipment.query.count(), 0)

    def test_reject_and_replace(self):
        order_id = self.get_order()
        self.staff_login()
        path = f"/tms/customer/orders/{order_id}/confirm"
        self.post(self.alice, path, {"revision": 1})
        self.assertEqual(self.decision(order_id, 2, decision="reject", reason="请补充资料").status_code, 302)
        self.assertIn("请补充资料", self.get(self.alice, f"/tms/customer/orders/{order_id}").get_data(as_text=True))
        self.assertEqual(self.upload(self.alice, replace=order_id, revision=2).status_code, 409)
        self.assertEqual(self.upload(self.alice, replace=order_id, revision=3).status_code, 302)
        self.assertEqual(self.post(self.alice, path, {"revision": 4}).status_code, 302)
        self.assertEqual(TmsOrder.query.count(), 1)

    def test_duplicate_file_number_and_legacy_number(self):
        self.get_order()
        self.assertEqual(self.upload(self.alice).status_code, 409)
        db.session.add(Shipment(customer_id=self.customer_a.id, tracking_number="fba-demo"))
        db.session.commit()
        self.assertEqual(self.upload(self.alice, workbook_bytes("FBA-DEMO")).status_code, 409)
        self.sign_in(self.bob, "customer-b")
        self.assertEqual(self.upload(self.bob, workbook_bytes(code="DEMO-B")).status_code, 302)
        self.assertEqual(TmsOrder.query.count(), 2)

    def test_admin_account_creation_csrf_and_portal_login(self):
        url = "/tms/admin/accounts"
        self.staff_login("tms-test-reader")
        self.assertEqual(self.get(self.staff, url).status_code, 403)
        self.staff_login()
        self.assertEqual(self.get(self.staff, url).status_code, 200)
        data = {"customer_id": self.customer_b.id, "username": "created-demo", "password": self.password}
        with app.app_context():
            self.assertEqual(self.staff.post(url, data=data).status_code, 400)
        self.assertEqual(self.post(self.staff, url, data, "/login").status_code, 302)
        account = TmsAccount.query.filter_by(username="created-demo").one()
        self.assertNotEqual(account.password_hash, self.password)
        self.assertTrue(check_password_hash(account.password_hash, self.password))
        self.assertEqual(self.sign_in(self.bob, "created-demo").status_code, 302)
        self.assertEqual(self.post(self.bob, "/tms/customer/logout", {}).status_code, 302)
        self.assertEqual(self.get(self.bob, "/tms/customer/").status_code, 302)

    def test_upload_security_and_disabled_flag(self):
        self.assertEqual(self.get(self.alice, "/tms/customer/import").status_code, 302)
        self.sign_in(self.alice, "customer-a")
        self.assertEqual(self.alice.post("/tms/customer/import", data={
            "invoice": (io.BytesIO(workbook_bytes()), "fictional.xlsx"),
        }).status_code, 400)
        self.assertEqual(self.upload(self.alice, b"x" * (3 * 1024 * 1024)).status_code, 413)
        response = self.get(self.alice, "/tms/customer/template")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        with patch.dict(app.config, TMS_ENABLED=False):
            self.assertEqual(self.get(self.alice, "/tms/customer/login").status_code, 404)
        self.assertEqual(TmsOrder.query.count(), 0)

    def test_additive_upgrade_idempotent(self):
        existing = self.customer_a.id
        with db.engine.begin() as connection:
            for table in (TmsAudit.__table__, TmsOrder.__table__, TmsAccount.__table__):
                table.drop(connection)
        runner = app.test_cli_runner()
        self.assertNotEqual(runner.invoke(args=["tms-upgrade-v1"]).exit_code, 0)
        for _ in range(2):
            self.assertEqual(runner.invoke(args=["tms-upgrade-v1", "--confirm"]).exit_code, 0)
        self.assertIsNotNone(db.session.get(Customer, existing))
        self.assertEqual(TmsOrder.query.count(), 0)
