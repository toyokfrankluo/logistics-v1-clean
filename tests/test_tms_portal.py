"""Phase 2 regression checks. All accounts, addresses and workbooks are synthetic."""
from base64 import b64decode
from datetime import datetime
from decimal import Decimal
import io
import os
from pathlib import Path
import re
import secrets
import tempfile
import unittest
from unittest.mock import patch
from xml.etree.ElementTree import SubElement, fromstring, tostring
from zipfile import ZipFile

from openpyxl import load_workbook

os.environ["LOGISTICS_ISOLATED"] = "1"

from werkzeug.security import check_password_hash, generate_password_hash
from app import app, User
from models import BankAccount, CarrierAgent, Customer, Shipment, db
from tms_invoice import InvoiceError, SERVICES, blank_invoice, parse_invoice
from tms_models import (TmsAccount, TmsAddressBook, TmsAgentApi, TmsAudit, TmsChargeAdjustment, TmsOrder, TmsReceivable,
                        TmsSupplierEvent, TmsSupplierOrder)
from tms_xlsx_images import XLSX_NS


PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j72sAAAAASUVORK5CYII=")
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
ART_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def workbook_bytes(order_number="0000123", code="DEMO-A", *, change=None, recipient="虚构地址甲", pictures=(23, 24, 25)):
    book = blank_invoice()
    address_sheet = book["地址库"]
    address_sheet.append(("虚构仓库", code, "", "", recipient, "", "", "", "1 Fictional Way",
                          "", "", "Test City", "CA", "US", "00001"))
    sheet = book["星睿发票"]
    for cell, value in {
        "D4": order_number, "D5": "UPS红单", "L4": code, "D7": "否",
        "D8": "否", "D9": "否", "D10": "否", "D11": "普通报关", "D14": "DEMO-PO", "D15": 2,
        "A23": "0001", "B23": "Demo item", "C23": "虚构商品", "G23": 1.25,
        "H23": 2, "J23": 1.5, "K23": 20, "L23": 10, "M23": 8, "N23": "否",
        "A24": "0001", "B24": "Second item", "C24": "虚构明细", "G24": 3,
        "H24": 1, "J24": 1.5, "K24": 20, "L24": 10, "M24": 8, "N24": "否",
        "A25": "0002", "B25": "Third item", "C25": "虚构另一箱", "G25": 2,
        "H25": 1, "J25": 1, "K25": 10, "L25": 10, "M25": 10, "N25": "否",
    }.items():
        sheet[cell] = value
    for line in (23, 24, 25):
        for col, value in {"D": "Paper", "E": "Demo", "F": "00123456"}.items():
            sheet[f"{col}{line}"] = value
        if line in pictures:
            sheet[f"O{line}"] = '=_xlfn.DISPIMG("ID_DEMO",1)'
    if change:
        change(book)
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return with_wps_photo(stream.getvalue())


def with_wps_photo(raw):
    target = io.BytesIO()
    with ZipFile(io.BytesIO(raw)) as original, ZipFile(target, "w") as output:
        for entry in original.infolist():
            output.writestr(entry, original.read(entry))
        output.writestr("xl/cellimages.xml", f"""
            <cellImages xmlns:xdr="{DRAWING_NS}" xmlns:a="{ART_NS}" xmlns:r="{REL_NS}">
              <cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr name="ID_DEMO"/></xdr:nvPicPr>
              <xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic></cellImage>
            </cellImages>""")
        output.writestr("xl/_rels/cellimages.xml.rels", f"""
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="{REL_NS}/image" Target="media/demo.png"/>
            </Relationships>""")
        output.writestr("xl/media/demo.png", PNG)
    return target.getvalue()


class InvoiceParsingTests(unittest.TestCase):
    def test_group_boxes_and_compute_without_formula_cache(self):
        raw = workbook_bytes()
        invoice = parse_invoice(raw)
        self.assertEqual(invoice["customer_number"], "0000123")
        self.assertEqual(invoice["address"]["recipient"], "虚构地址甲")
        self.assertEqual(invoice["address"]["postal_code"], "00001")
        self.assertEqual([item["box_number"] for item in invoice["boxes"]], ["0001", "0002"])
        self.assertEqual(len(invoice["declarations"]), 3)
        self.assertEqual(invoice["declarations"][0]["total_usd"], "2.50")
        self.assertEqual(len(SERVICES), 13)

    def test_unknown_catalogue_and_duplicate_address_code(self):
        with self.assertRaisesRegex(InvoiceError, "不存在"):
            parse_invoice(workbook_bytes(change=lambda book: book["星睿发票"].__setitem__("L4", "DEMO-B")))
        def duplicate(book):
            sheet = book["地址库"]
            row = [cell.value for cell in sheet[2]]
            row[1] = " demo-a "
            sheet.append(row)
        with self.assertRaisesRegex(InvoiceError, "重复编码"):
            parse_invoice(workbook_bytes(change=duplicate))

    def test_all_thirteen_services_with_product_rows(self):
        for service in SERVICES:
            with self.subTest(service=service):
                raw = workbook_bytes(change=lambda book: book["星睿发票"].__setitem__("D5", service))
                invoice = parse_invoice(raw)
                self.assertEqual(invoice["service"], service)
                self.assertEqual(len(invoice["declarations"]), 3)

    def test_three_sheets_address_columns_and_country_format(self):
        for change, message in (
            (lambda book: book.remove(book["服务选项"]), "三工作表"),
            (lambda book: book["地址库"].__setitem__("N1", "其他"), "地址库表头"),
            (lambda book: book["地址库"].__setitem__("N2", "USA"), "两位字母"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(InvoiceError, message):
                parse_invoice(workbook_bytes(change=change))

    def test_conflicting_literal_or_cached_recipient_is_rejected(self):
        with self.assertRaisesRegex(InvoiceError, "L7.*冲突"):
            parse_invoice(workbook_bytes(change=lambda book: book["星睿发票"].__setitem__("L7", "Different street")))
        for shown, conflict in ((" 1  FICTIONAL Way ", False), ("Different street", True)):
            with self.subTest(conflict=conflict):
                output = io.BytesIO()
                with ZipFile(io.BytesIO(workbook_bytes())) as source, ZipFile(output, "w") as archive:
                    for entry in source.infolist():
                        data = source.read(entry)
                        if entry.filename == "xl/worksheets/sheet1.xml":
                            xml = fromstring(data)
                            cell = xml.find(f".//{XLSX_NS}c[@r='L7']")
                            cell.set("t", "str")
                            cell.find(XLSX_NS + "v").text = shown
                            data = tostring(xml)
                        archive.writestr(entry, data)
                if conflict:
                    with self.assertRaisesRegex(InvoiceError, "L7.*冲突"):
                        parse_invoice(output.getvalue())
                else:
                    self.assertEqual(parse_invoice(output.getvalue())["address"]["street"], "1 Fictional Way")

    def test_bad_types_and_format(self):
        for cell, value in (("D4", 123), ("D5", "无效服务"), ("G23", "=1+1"),
                            ("N23", "也许"), ("D15", 3), ("J23", -1)):
            with self.subTest(cell=cell), self.assertRaises(InvoiceError):
                raw = workbook_bytes(change=lambda book: book["星睿发票"].__setitem__(cell, value))
                parse_invoice(raw)
        with self.assertRaises(InvoiceError):
            parse_invoice(b"not an xlsx")

    def test_missing_starred_fields_report_cell_coordinates(self):
        def blanks(book):
            for cell in ("D9", "D11", "D14", "D15", "B23", "C23", "D23", "E23", "F23", "J24"):
                book["星睿发票"][cell] = None
        with self.assertRaises(InvoiceError) as raised:
            parse_invoice(workbook_bytes(change=blanks, pictures=(24, 25)))
        for cell in ("D9", "D11", "D14", "D15", "B23", "C23", "D23", "E23", "F23", "J24", "O23"):
            self.assertIn(f"{cell}（", str(raised.exception))
        self.assertNotIn("O26", str(raised.exception))

    def test_missing_recipient_field_comes_from_embedded_catalogue(self):
        def no_region(book):
            book["地址库"]["M2"] = None
        with self.assertRaisesRegex(InvoiceError, "L11（省份 / 州）"):
            parse_invoice(workbook_bytes(change=no_region))

    def test_wps_photo_and_number_formatted_hs_code(self):
        def format_code(book):
            cell = book["星睿发票"]["F23"]
            cell.value = 1234
            cell.number_format = "000000"
        invoice = parse_invoice(workbook_bytes(change=format_code))
        self.assertEqual(invoice["declarations"][0]["hs_code"], "001234")
        self.assertEqual(invoice["declarations"][0]["image_part"], "xl/media/demo.png")

    def test_excel_drawing_photo_anchored_in_column_o(self):
        def relationships(kind, target):
            return f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL_NS}/{kind}" Target="{target}"/></Relationships>'

        raw = workbook_bytes(pictures=())
        output = io.BytesIO()
        sheet = "xl/worksheets/sheet1.xml"
        with ZipFile(io.BytesIO(raw)) as source, ZipFile(output, "w") as archive:
            for entry in source.infolist():
                data = source.read(entry)
                if entry.filename == sheet:
                    xml = fromstring(data)
                    SubElement(xml, XLSX_NS + "drawing", {"{" + REL_NS + "}id": "rId1"})
                    data = tostring(xml)
                archive.writestr(entry, data)
            archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", relationships("drawing", "../drawings/drawing1.xml"))
            archive.writestr("xl/drawings/_rels/drawing1.xml.rels", relationships("image", "../media/demo.png"))
            anchors = "".join(f"""<xdr:oneCellAnchor>
                <xdr:from><xdr:col>14</xdr:col><xdr:row>{row}</xdr:row></xdr:from>
                <xdr:pic><xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic>
                </xdr:oneCellAnchor>""" for row in (22, 23, 24))
            archive.writestr("xl/drawings/drawing1.xml",
                             f'<xdr:wsDr xmlns:xdr="{DRAWING_NS}" xmlns:a="{ART_NS}" xmlns:r="{REL_NS}">{anchors}</xdr:wsDr>')
        invoice = parse_invoice(output.getvalue())
        self.assertEqual([item["image_part"] for item in invoice["declarations"]], ["xl/media/demo.png"] * 3)

    def test_declared_total_rounds_like_invoice(self):
        def fractions(book):
            book["星睿发票"]["G23"] = 1.005
            book["星睿发票"]["H23"] = 1
        self.assertEqual(parse_invoice(workbook_bytes(change=fractions))
                         ["declarations"][0]["total_usd"], "1.01")

    def test_wps_photo_must_reference_internal_image_from_product_cell(self):
        for cell, formula, message in (
            ("O23", '=_xlfn.DISPIMG("ID_MISSING",1)', "O23（产品图片）"),
            ("O23", '=_xlfn.IMAGE("https://example.invalid")', "公式函数"),
            ("B23", '=_xlfn.DISPIMG("ID_DEMO",1)', "公式函数"),
        ):
            with self.subTest(cell=cell, formula=formula), self.assertRaises(InvoiceError) as raised:
                parse_invoice(workbook_bytes(change=lambda book: book["星睿发票"].__setitem__(cell, formula)))
            self.assertIn(message, str(raised.exception))

    def test_active_formula_and_conflicting_box_measurements_rejected(self):
        def formula(book):
            book["地址库"]["P2"] = '=WEBSERVICE("https://example.invalid")'
        def conflict(book):
            for cell, value in {"J24": 4, "K24": 20, "L24": 10, "M24": 8}.items():
                book["星睿发票"][cell] = value
        with self.assertRaisesRegex(InvoiceError, "公式函数"):
            parse_invoice(workbook_bytes(change=formula))
        with self.assertRaisesRegex(InvoiceError, "重复箱号"):
            parse_invoice(workbook_bytes(change=conflict))


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

    def pricing(self, **changes):
        return dict(agent_id=self.agent.id, agent_service="虚构代理渠道",
                    unit="kg", quantity="100", quantity_source="declared", rate="12",
                    surcharge="0", operation="0", high_value="0", currency="CNY",
                    other_mode="none", confirmed="yes", destination_confirmed="yes") | changes

    def pending_order(self):
        order_id = self.get_order()
        self.assertEqual(self.post(self.alice, f"/tms/customer/orders/{order_id}/confirm",
                                   {"revision": 1}).status_code, 302)
        self.assertEqual(self.staff_login().status_code, 302)
        return order_id

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
        self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing(agent_service="内部代理服务代码")).status_code, 302)
        self.assertEqual(self.decision(order_id, 2, decision="reject", reason="重复").status_code, 409)
        db.session.expire_all()
        self.assertEqual(db.session.get(TmsOrder, order_id).status, "approved")
        self.assertEqual(TmsAudit.query.count(), 3)
        self.assertNotIn("内部代理服务代码", self.get(self.alice, f"/tms/customer/orders/{order_id}").get_data(as_text=True))
        self.assertEqual(Shipment.query.count(), 1)
        charge = db.session.get(TmsReceivable, order_id)
        self.assertEqual(charge.total, Decimal("1200.00"))
        shipment = db.session.get(Shipment, charge.shipment_id)
        self.assertEqual((shipment.customer_id, shipment.agent_id), (self.customer_a.id, self.agent.id))
        self.assertIsNone(shipment.shipment_id)
        self.assertIsNone(shipment.third_party_tracking_number)

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

    def test_account_errors_stay_on_form_and_chinese_account_can_sign_in(self):
        url = "/tms/admin/accounts"
        self.staff_login()
        data = {"customer_id": self.customer_b.id, "username": "星睿", "password": "short"}
        response = self.post(self.staff, url, data, "/login")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 422)
        self.assertIn('id="password-error"', html)
        self.assertNotIn('id="username-error"', html)
        self.assertIn(f'value="{self.customer_b.id}" selected', html)
        self.assertIn('value="星睿"', html)
        self.assertNotIn('value="short"', html)
        self.assertIsNone(TmsAccount.query.filter_by(username="星睿").first())

        data["password"] = self.password
        self.assertEqual(self.post(self.staff, url, data, "/login").status_code, 302)
        self.assertIn("星睿", self.get(self.staff, url).get_data(as_text=True))
        self.assertEqual(self.sign_in(self.bob, " 星睿 ").location, "/tms/customer/")
        conflict = self.post(self.staff, url, data, "/login")
        self.assertEqual(conflict.status_code, 409)
        self.assertIn('id="username-error"', conflict.get_data(as_text=True))

    def test_account_selection_and_invalid_username_have_separate_errors(self):
        self.staff_login()
        response = self.post(self.staff, "/tms/admin/accounts",
                             {"customer_id": 2 ** 100, "username": "非法 账号", "password": self.password},
                             "/login")
        self.assertEqual(response.status_code, 422)
        html = response.get_data(as_text=True)
        self.assertIn('id="customer-error"', html)
        self.assertIn('id="username-error"', html)
        self.assertNotIn('id="password-error"', html)

    def test_list_filters_keep_customer_scope_and_use_business_dates(self):
        order_id = self.get_order()
        another = self.upload(self.alice, workbook_bytes("OTHER-001"))
        other_id = int(another.location.rsplit("/", 1)[1])
        db.session.get(TmsOrder, order_id).created_at = datetime(2026, 11, 7, 16)
        db.session.get(TmsOrder, other_id).created_at = datetime(2026, 11, 8, 16)
        db.session.commit()
        response = self.get(self.alice, "/tms/customer/?created_from=2026-11-08&created_to=2026-11-08"
                            "&country=US&address=DEMO-A&recipient=虚构地址&service=UPS红单")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'href="/tms/customer/orders/{order_id}"', html)
        self.assertNotIn(f'href="/tms/customer/orders/{other_id}"', html)
        self.assertIn("2026-11-08 00:00", html)
        self.assertIn("7.50", html)
        self.assertIn("2.5", html)
        response = self.get(self.alice, f"/tms/customer/?q=XR-{order_id}，NO-MATCH")
        self.assertIn(f'href="/tms/customer/orders/{order_id}"', response.get_data(as_text=True))
        self.sign_in(self.bob, "customer-b")
        response = self.get(self.bob, f"/tms/customer/?q=XR-{order_id}")
        self.assertIn("当前没有符合条件的订单", response.get_data(as_text=True))
        response = self.get(self.alice, "/tms/customer/?created_from=2026-11-09&created_to=2026-11-08")
        self.assertEqual(response.status_code, 400)

    def test_drawer_reuses_authorized_detail_and_hides_internal_fields(self):
        order_id = self.get_order()
        self.post(self.alice, f"/tms/customer/orders/{order_id}/confirm", {"revision": 1})
        self.staff_login()
        self.decision(order_id, 2, decision="approve", **self.pricing(agent_service="后台专用渠道"))
        drawer = self.get(self.alice, f"/tms/customer/orders/{order_id}?drawer=1")
        html = drawer.get_data(as_text=True)
        self.assertIn("data-order-detail", html)
        self.assertIn("提交审核", html)
        self.assertIn("暂无物流轨迹", html)
        self.assertNotIn("<!doctype html>", html)
        self.assertNotIn("后台专用渠道", html)
        self.sign_in(self.bob, "customer-b")
        self.assertEqual(self.get(self.bob, f"/tms/customer/orders/{order_id}?drawer=1").status_code, 404)
        staff = self.get(self.staff, f"/tms/admin/orders/{order_id}?drawer=1")
        self.assertIn("后台专用渠道", staff.get_data(as_text=True))

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

    def test_download_preserves_layout_and_includes_only_assigned_catalogue(self):
        self.sign_in(self.alice, "customer-a")
        directory = app.config["TMS_ADDRESS_BOOK_DIR"]
        layout = Path(directory, f"{self.customer_b.id}.xlsx")
        with patch.dict(app.config, TMS_INVOICE_TEMPLATE_PATH=str(layout)):
            response = self.get(self.alice, "/tms/customer/template")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        with ZipFile(io.BytesIO(response.data)) as archive:
            self.assertFalse(any(name.startswith("xl/media/") for name in archive.namelist()))
        book = load_workbook(io.BytesIO(response.data))
        self.addCleanup(book.close)
        self.assertEqual(book.sheetnames, ["星睿发票", "地址库", "服务选项"])
        self.assertEqual([row[0] for row in book["地址库"].iter_rows(min_row=2, min_col=2, max_col=2, values_only=True)],
                         ["DEMO-A"])
        sheet = book["星睿发票"]
        self.assertIn("L4:N4", sheet.merged_cells)
        self.assertIn("地址库!", sheet["L7"].value)
        self.assertTrue(any("L4" in item and "地址库" in item.formula1 for item in sheet.data_validations.dataValidation))
        self.assertEqual(sheet["O22"].value, "产品图片*")
        self.assertIsNone(sheet["D4"].value)
        self.assertIsNone(sheet["O23"].value)

    def test_database_catalogue_workflow_without_filesystem(self):
        path = f"/tms/admin/accounts/{self.customer_a.id}/address-book"
        self.assertEqual(self.staff_login().status_code, 302)
        with patch.dict(app.config, TMS_ADDRESS_BOOK_DIR=None):
            self.assertEqual(self.get(self.staff, path).status_code, 200)
            saved = self.post(self.staff, path, {
                "catalogue": (io.BytesIO(workbook_bytes(recipient="虚构授权地址")), "catalogue.xlsx"),
            }, "/login")
            self.assertEqual(saved.status_code, 302)
            db.session.expire_all()
            self.assertEqual(len(db.session.get(TmsAddressBook, self.customer_a.id).rows), 1)
            self.assertEqual(self.sign_in(self.alice, "customer-a").status_code, 302)
            downloaded = self.get(self.alice, "/tms/customer/template")
            book = load_workbook(io.BytesIO(downloaded.data))
            self.addCleanup(book.close)
            self.assertEqual(book["地址库"]["E2"].value, "虚构授权地址")
            self.assertIn("地址库!", book["星睿发票"]["L7"].value)
            response = self.upload(self.alice, workbook_bytes(recipient="虚构客户申报"))
            self.assertEqual(response.status_code, 302)
            order_id = int(response.location.rsplit("/", 1)[1])
            preview = self.get(self.alice, response.location).get_data(as_text=True)
            self.assertIn("虚构客户申报", preview)
            self.assertNotIn("虚构授权地址", preview)
            self.sign_in(self.bob, "customer-b")
            self.assertEqual(self.get(self.bob, "/tms/customer/template").status_code, 400)
            self.assertEqual(self.upload(self.bob).status_code, 302)
            self.assertEqual(self.get(self.bob, response.location).status_code, 404)
            self.assertEqual(self.post(self.alice, f"/tms/customer/orders/{order_id}/confirm",
                                       {"revision": 1}).status_code, 302)
            review = self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True)
            self.assertIn("公司参考地址库与客户申报不一致", review)
            self.assertIn("公司参考 虚构授权地址", review)
            self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing(confirmed="")).status_code, 400)
            self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing(destination_confirmed="")).status_code, 400)
            charges = self.pricing(surcharge="3", operation="4", high_value="5", other_mode="items",
                                   other_name=["虚构附加项目"], other_amount=["6"], other_currency=["CNY"], other_note=[""])
            self.assertEqual(self.decision(order_id, 2, decision="approve",
                                           **(charges | {"destination_recipient": "虚构更正"})).status_code, 400)
            self.assertEqual(self.decision(order_id, 2, decision="approve",
                                           **(charges | {"destination_country": "USA"})).status_code, 400)
            charges.update(destination_recipient="虚构更正", destination_country="CA",
                           destination_reason="虚构人工复核")
            self.assertEqual(self.decision(order_id, 2, decision="approve", **charges).status_code, 302)
            receivable = db.session.get(TmsReceivable, order_id)
            self.assertEqual(receivable.total, Decimal("1218.00"))
            self.assertEqual(db.session.get(Shipment, receivable.shipment_id).destination, "CA")
            self.assertEqual(TmsSupplierOrder.query.count(), 0)
            self.carrier_post.assert_not_called()
            order = db.session.get(TmsOrder, order_id)
            self.assertEqual(order.invoice["submitted_destination"]["address"]["recipient"], "虚构客户申报")
            self.assertTrue(order.invoice["destination_review"]["changed"])
            self.assertEqual(order.invoice["destination_review"]["reason"], "虚构人工复核")
            replacement = self.post(self.staff, path, {
                "catalogue": (io.BytesIO(workbook_bytes(recipient="更新后公司参考地址")), "catalogue.xlsx"),
            }, "/login")
            self.assertEqual(replacement.status_code, 302)
            db.session.expire_all()
            self.assertEqual(order.invoice["address"]["recipient"], "虚构更正")
            customer_view = self.get(self.alice, response.location).get_data(as_text=True)
            self.assertIn("虚构更正", customer_view)
            self.assertNotIn("虚构人工复核", customer_view)
            self.assertNotIn("更新后公司参考地址", customer_view)

    def test_address_book_admin_csrf_and_atomic_replacement(self):
        path = f"/tms/admin/accounts/{self.customer_a.id}/address-book"
        self.assertEqual(self.get(self.staff, path).status_code, 302)
        self.staff_login("tms-test-reader")
        self.assertEqual(self.get(self.staff, path).status_code, 403)
        self.staff_login()
        self.assertEqual(self.staff.post(path, data={"catalogue": (io.BytesIO(workbook_bytes()), "book.xlsx")}).status_code, 400)
        def submit(raw):
            return self.post(self.staff, path, {"catalogue": (io.BytesIO(raw), "book.xlsx")}, "/login")
        self.assertEqual(submit(workbook_bytes()).status_code, 302)
        def duplicate(book):
            book["地址库"].append([cell.value for cell in book["地址库"][2]])
        self.assertEqual(submit(workbook_bytes(change=duplicate)).status_code, 400)
        self.assertEqual(db.session.get(TmsAddressBook, self.customer_a.id).rows[0][1], "DEMO-A")
        self.assertEqual(submit(workbook_bytes(code="DEMO-C")).status_code, 302)
        self.sign_in(self.alice, "customer-a")
        self.assertEqual(self.upload(self.alice).status_code, 302)
        self.assertEqual(self.upload(self.alice, workbook_bytes("SECOND-DEMO", code="DEMO-C")).status_code, 302)

    def test_validation_stops_preview_and_creates_no_order(self):
        self.sign_in(self.alice, "customer-a")
        raw = workbook_bytes(change=lambda book: book["星睿发票"].__setitem__("D9", None))
        response = self.upload(self.alice, raw)
        self.assertEqual(response.status_code, 400)
        self.assertIn("D9（是否带液体）", response.get_data(as_text=True))
        self.assertEqual(TmsOrder.query.count(), 0)

    def test_photo_visible_in_preview_and_protected_by_order_permissions(self):
        order_id = self.get_order()
        path = f"/tms/customer/orders/{order_id}/photos/0"
        preview = self.get(self.alice, f"/tms/customer/orders/{order_id}")
        self.assertIn(path, preview.get_data(as_text=True))
        image = self.get(self.alice, path)
        self.assertEqual(image.content_type, "image/png")
        self.assertEqual(image.data, PNG)
        self.assertIn("no-store", image.headers["Cache-Control"])
        self.assertEqual(self.get(self.alice, path[:-1] + "9").status_code, 404)
        self.assertEqual(self.get(self.bob, path).status_code, 302)
        self.sign_in(self.bob, "customer-b")
        self.assertEqual(self.get(self.bob, path).status_code, 404)
        self.staff_login()
        staff_path = f"/tms/admin/orders/{order_id}/photos/0"
        self.assertEqual(self.get(self.staff, staff_path).status_code, 404)
        self.post(self.alice, f"/tms/customer/orders/{order_id}/confirm", {"revision": 1})
        self.assertEqual(self.get(self.staff, staff_path).data, PNG)

    def test_required_pricing_fields_and_explicit_zero(self):
        order_id = self.pending_order()
        cases = (("agent_id", "", "启用的实际物流代理"), ("agent_service", "", "实际代理运输渠道"),
                 ("rate", "", "计费单价"), ("quantity", "0", "计费数量"),
                 ("quantity", "NaN", "计费数量"), ("quantity_source", "", "计费数量来源"),
                 ("operation", "", "操作费"), ("surcharge", "", "附加费"),
                 ("high_value", "-1", "超值费"), ("other_mode", "", "无其他费用"),
                 ("confirmed", "", "请确认收费"))
        for field, value, error in cases:
            with self.subTest(field=field, value=value):
                res = self.decision(order_id, 2, decision="approve", **self.pricing(**{field: value}))
                self.assertEqual(res.status_code, 400)
                self.assertIn(error, res.get_data(as_text=True))
        self.assertEqual(Shipment.query.count(), 0)
        self.assertEqual(TmsReceivable.query.count(), 0)
        self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing()).status_code, 302)

    def test_draft_remove_fee_then_approve_finance_and_excel(self):
        order_id = self.pending_order()
        db.session.add_all([
            Shipment(customer_id=self.customer_a.id, tracking_number="LEGACY-EXAMPLE", fee=3),
            BankAccount(bank_name="虚构演示银行", account_name="虚构公司", account_no="DEMO-0000", is_default=True),
        ])
        db.session.commit()
        values = self.pricing(currency="USD", operation="7", other_mode="items", other_name=["偏远派送费", "测试待删除"],
                              other_amount=["10.10", "22.00"], other_currency=["USD", "USD"], other_note=["", ""])
        self.assertEqual(self.decision(order_id, 2, decision="draft", **values).status_code, 302)
        draft = db.session.get(TmsReceivable, order_id)
        self.assertIsNone(draft.shipment_id)
        self.assertEqual(len(draft.details["other"]), 2)
        values.update(other_name=["偏远派送费"], other_amount=["10.10"], other_currency=["EUR"], other_note=[""])
        self.assertEqual(self.decision(order_id, 3, decision="approve", **values).status_code, 400)
        values["other_currency"] = ["USD"]
        self.assertEqual(self.decision(order_id, 3, decision="approve", **values).status_code, 302)
        self.assertEqual(self.decision(order_id, 3, decision="approve", **values).status_code, 409)
        db.session.expire_all()
        row = db.session.get(TmsReceivable, order_id)
        self.assertEqual(row.total, Decimal("1217.10"))
        self.assertEqual(len(row.details["other"]), 1)
        self.assertEqual(Shipment.query.count(), 2)
        body = self.get(self.staff, "/finance").get_data(as_text=True)
        self.assertIn("1217.10 USD", body)
        self.assertIn("3.00 CNY", body)
        result = self.post(self.staff, "/finance", {"export": "1"}, "/login")
        self.assertEqual(result.status_code, 200)
        workbook = load_workbook(io.BytesIO(result.data))
        self.addCleanup(workbook.close)
        fees = list(workbook["TMS 收费明细"].values)
        self.assertIn(("0000123", "偏远派送费", "USD", 10.1), fees)
        self.assertNotIn("测试待删除", str(fees))
        self.assertIn(("0000123", "应收总额", "USD", 1217.1), fees)
        self.assertIn("账号：DEMO-0000", [cell[0] for cell in workbook["账单"].values])
        body = self.get(self.staff, "/shipments?review=legacy").get_data(as_text=True)
        self.assertIn("LEGACY-EXAMPLE", body)
        self.assertNotIn("0000123", body)

    def test_adjustments_preserve_history_and_linked_mutations_are_guarded(self):
        order_id = self.pending_order()
        self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing()).status_code, 302)
        receivable = db.session.get(TmsReceivable, order_id)
        shipment_id = receivable.shipment_id
        self.assertEqual(self.decision(order_id, 3, decision="adjust", **self.pricing(rate="13")).status_code, 400)
        response = self.decision(order_id, 3, decision="adjust", **self.pricing(
            rate="13", adjustment_reason="虚构实测更正", review_note="仅管理端备注", quantity_source="warehouse"))
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        history = TmsChargeAdjustment.query.one()
        self.assertEqual((history.previous["total"], history.revised["total"]), ("1200.00", "1300.00"))
        self.assertEqual(history.reason, "虚构实测更正")
        self.assertIsNotNone(history.actor_id)
        self.assertIsNotNone(history.created_at)
        for url in (f"/shipments/edit/{shipment_id}", f"/shipments/{shipment_id}/edit"):
            self.assertEqual(self.post(self.staff, url, {"fee": "1"}, "/login").status_code, 409)
        self.assertEqual(self.post(self.staff, f"/shipments/delete/{shipment_id}", {}, "/login").status_code, 409)
        customer_view = self.get(self.alice, f"/tms/customer/orders/{order_id}").get_data(as_text=True)
        self.assertIn("1300.00 CNY", customer_view)
        self.assertNotIn("仅管理端备注", customer_view)
        self.assertNotIn("虚构实测更正", customer_view)
        self.assertIn("1300.00 CNY", self.get(self.staff, "/finance").get_data(as_text=True))

    def test_duplicate_legacy_reference_and_commit_failure_do_not_approve(self):
        order_id = self.pending_order()
        shipment = Shipment(customer_id=self.customer_a.id, tracking_number="0000123")
        db.session.add(shipment)
        db.session.commit()
        self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing()).status_code, 409)
        db.session.delete(shipment)
        db.session.commit()
        from sqlalchemy.exc import SQLAlchemyError
        with patch.object(db.session, "commit", side_effect=SQLAlchemyError):
            self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing()).status_code, 409)
        self.assertEqual(Shipment.query.count(), 0)
        self.assertEqual(TmsReceivable.query.count(), 0)
        self.assertEqual(db.session.get(TmsOrder, order_id).status, "pending")

    def test_shared_navigation_and_account_deactivation(self):
        self.staff_login()
        for path in ("/tms/admin/", "/tms/admin/accounts", "/shipments", "/agents", "/finance", "/bank_accounts", "/track"):
            response = self.get(self.staff, path)
            self.assertEqual(response.status_code, 200, path)
            html = response.get_data(as_text=True)
            self.assertIn('class="tms-admin"', html, path)
            self.assertIn("收款账户管理", html, path)
            self.assertNotIn("录入运单</a>", html, path)
            self.assertNotIn("客户管理</a>", html, path)
        account_id = TmsAccount.query.filter_by(username="customer-a").one().id
        path = f"/tms/admin/accounts/{account_id}/availability"
        self.sign_in(self.alice, "customer-a")
        self.assertEqual(self.staff.post(path, data={"state": "disable"}).status_code, 400)
        self.assertEqual(self.post(self.staff, path, {"state": "disable"}, "/login").status_code, 302)
        self.assertEqual(self.get(self.alice, "/tms/customer/").status_code, 302)
        self.staff_login("tms-test-reader")
        self.assertEqual(self.post(self.staff, path, {"state": "enable"}, "/login").status_code, 403)

    def test_billing_migration_preserves_old_approved_order_and_legacy_fee(self):
        order_id = self.pending_order()
        order = db.session.get(TmsOrder, order_id)
        order.status = "approved"
        db.session.add(Shipment(customer_id=self.customer_a.id, tracking_number="HISTORIC-FEE", fee=25))
        db.session.commit()
        with db.engine.begin() as connection:
            TmsChargeAdjustment.__table__.drop(connection)
            TmsReceivable.__table__.drop(connection)
        self.assertIn("25.00 CNY", self.get(self.staff, "/finance").get_data(as_text=True))
        result = app.test_cli_runner().invoke(args=["tms-upgrade-v1", "--confirm"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(db.session.get(TmsOrder, order_id).status, "approved")
        self.assertEqual(TmsReceivable.query.count(), 0)
        self.assertIn("25.00 CNY", self.get(self.staff, "/finance").get_data(as_text=True))

    def test_corrected_invoice_discards_stale_pricing_draft(self):
        order_id = self.pending_order()
        self.assertEqual(self.decision(order_id, 2, decision="draft", **self.pricing()).status_code, 302)
        self.assertEqual(self.decision(order_id, 3, decision="reject", reason="请更正重量").status_code, 302)
        response = self.upload(self.alice, workbook_bytes(change=lambda book:
            book["星睿发票"].__setitem__("J25", 3)), replace=order_id, revision=4)
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(TmsReceivable, order_id))

    def test_additive_upgrade_idempotent(self):
        existing = self.customer_a.id
        db.session.remove()
        with db.engine.begin() as connection:
            for model in (TmsSupplierEvent, TmsSupplierOrder, TmsAgentApi, TmsChargeAdjustment,
                          TmsReceivable, TmsAudit, TmsOrder, TmsAccount, TmsAddressBook):
                model.__table__.drop(connection)
        runner = app.test_cli_runner()
        self.assertNotEqual(runner.invoke(args=["tms-upgrade-v1"]).exit_code, 0)
        for _ in range(2):
            self.assertEqual(runner.invoke(args=["tms-upgrade-v1", "--confirm"]).exit_code, 0)
        self.assertIsNotNone(db.session.get(Customer, existing))
        self.assertEqual(TmsOrder.query.count(), 0)
        self.assertEqual(TmsAddressBook.query.count(), 0)
