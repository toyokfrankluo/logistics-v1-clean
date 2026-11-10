"""NextSLS fixtures use synthetic orders and an entirely simulated carrier transport."""
from datetime import datetime, timedelta
from functools import partial
from io import BytesIO
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

import carrier_http
import nextsls
import test_tms_portal as fixture
import tms_carriers
from app import app
from models import BankAccount, CarrierAgent, Customer, ManualTrack, Shipment, db
from tms_models import TmsAccount, TmsAgentApi, TmsAudit, TmsOrder, TmsReceivable, TmsSupplierEvent, TmsSupplierOrder


ORIGIN = "https://demo.nextsls.com"
SUPPLIER_NUMBER = "TEST-SLS-1"
TOKEN = "synthetic-test-token"
SERVICE = {
    "code": "TMS_DEMO", "name": "虚构代理标准线", "require_declaration_list": ["name_en", "unit_value", "qty"],
    "from_address": "no", "tel_required": 0, "email_required": 0, "company_required": 0,
    "city_required": 1, "vat_number_required": 0, "eori_required": 0, "packing_list_required": 0,
}


def success(data):
    return SimpleNamespace(status_code=200, json=lambda: {"status": 1, "data": data})


def snapshot(number=SUPPLIER_NUMBER, charge="13.20"):
    return {"shipment": {
        "shipment_id": number, "client_reference": "0000123",
        "chargeable_weight": "12", "picked_count": 2,
        "actual_volume": "30.5", "volume_weight": 7, "volume": "1.02",
        "parcels": [{"number": "TEST-BOX", "ext_number": "BOX-EXT", "client_weight": 20,
                     "chargeable_weight": 12, "chargeable_length": 20}],
        "charge_list": [{"currency": "USD", "charge": charge, "charge_type": "=literal",
                         "exchange_rate": "7.2", "invoice_time": 123, "unit": "piece", "unit_price": charge}],
    }}


class NextSLSTests(unittest.TestCase):
    setUp = fixture.PortalTests.setUp
    drop_tables = fixture.PortalTests.drop_tables
    post = fixture.PortalTests.post
    get = fixture.PortalTests.get
    get_order = fixture.PortalTests.get_order
    staff_login = fixture.PortalTests.staff_login
    sign_in = fixture.PortalTests.sign_in
    upload = fixture.PortalTests.upload
    decision = fixture.PortalTests.decision
    pricing = fixture.PortalTests.pricing
    settle = fixture.PortalTests.settle
    pending_order = fixture.PortalTests.pending_order

    def approve(self, **pricing):
        order_id = self.pending_order()
        self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing(
            agent_service="TMS_DEMO", **pricing)).status_code, 302)
        return order_id

    def profile(self, *, rules=None, **changes):
        agent = db.session.get(CarrierAgent, self.agent.id)
        agent.app_token = TOKEN
        agent.api_url = ORIGIN + "/api/v5/shipment/get_tracking"
        settings = dict(provider="nextsls", base_url=ORIGIN,
                        enabled=["create", "reconcile", "measurement", "parcels", "charges"],
                        service_codes=["TMS_DEMO"], sender={}, verified={})
        profile = TmsAgentApi(agent_id=agent.id, **(settings | changes))
        if profile.service_codes:
            tms_carriers.store_channel_catalogue(
                profile, {code: SERVICE["name"] for code in profile.service_codes},
                rules if rules is not None else {code: nextsls.preparation_rules(SERVICE) for code in profile.service_codes},
            )
        db.session.add(profile)
        db.session.commit()
        return profile

    def action(self, order_id, action, **form):
        if action == "create":
            form.setdefault("revision", db.session.get(TmsOrder, order_id).revision)
        return self.post(self.staff, f"/tms/admin/orders/{order_id}/supplier",
                         {"action": action, **form}, "/login")

    def test_agent_config_keeps_legacy_and_hides_credentials(self):
        self.staff_login()
        self.agent.api_url = ORIGIN + "/api/v5/shipment/get_tracking"
        self.agent.app_token = TOKEN
        self.agent.app_key = "hidden-synthetic-key"
        self.agent.customer_code = "hidden-synthetic-account"
        db.session.commit()
        url = f"/agents/{self.agent.id}/edit"
        result = self.post(self.staff, url, {
            "provider": "nextsls", "base_url": ORIGIN, "service_codes": "TMS_DEMO",
            "capability": ["measurement", "charges"],
        }, "/login")
        self.assertEqual(result.status_code, 302)
        self.assertEqual(db.session.get(TmsAgentApi, self.agent.id).enabled, ["charges", "measurement"])
        self.assertEqual(self.agent.app_token, TOKEN)
        self.assertEqual(self.agent.api_url, ORIGIN + "/api/v5/shipment/get_tracking")
        body = self.get(self.staff, url).get_data(as_text=True)
        for secret in (TOKEN, "hidden-synthetic-key", "hidden-synthetic-account"):
            self.assertNotIn(secret, body)
        self.assertIn("文档确认", body)
        self.assertIn("未验证", body)
        for path in ("/admin/carrieragent/", f"/admin/carrieragent/edit/?id={self.agent.id}"):
            admin_body = self.get(self.staff, path).get_data(as_text=True)
            for secret in (TOKEN, "hidden-synthetic-key", "hidden-synthetic-account"):
                self.assertNotIn(secret, admin_body)
        self.post(self.staff, url, {"provider": "nextsls", "base_url": "https://127.0.0.1",
                                    "capability": "create"}, "/login")
        self.assertEqual(db.session.get(TmsAgentApi, self.agent.id).base_url, ORIGIN)

    def test_default_gate_confirmation_and_admin_permissions(self):
        order_id = self.approve()
        self.profile()
        blocked = self.action(order_id, "create", confirm="yes")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("系统未开放真实下单", blocked.get_data(as_text=True))
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))
        self.assertEqual(self.staff.post(f"/tms/admin/orders/{order_id}/supplier",
                                         data={"action": "create"}).status_code, 400)
        self.staff_login("tms-test-reader")
        self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 403)
        self.assertEqual(self.get(self.alice, f"/tms/admin/orders/{order_id}/supplier/measurements.xlsx").status_code, 302)

    def test_approval_cannot_call_create_even_when_feature_is_enabled(self):
        self.profile()
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True), patch("carrier_http.post") as transport:
            order_id = self.approve()
            transport.assert_not_called()
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))
        receivable = db.session.get(TmsReceivable, order_id)
        self.assertIsNone(db.session.get(Shipment, receivable.shipment_id).shipment_id)

    def test_approved_excel_creates_once_and_keeps_distinct_identifiers(self):
        order_id = self.pending_order()
        self.assertEqual(self.post(self.staff, "/agents", {
            "name": "虚构 API 代理", "provider": "nextsls", "base_url": ORIGIN,
            "app_token": TOKEN, "service_codes": "TMS_DEMO", "capability": "create",
        }, "/login").status_code, 302)
        agent = CarrierAgent.query.filter_by(name="虚构 API 代理").one()
        self.assertFalse(agent.api_url)
        with patch("carrier_http.post", return_value=success({"services": [SERVICE]})):
            self.assertEqual(self.post(self.staff, f"/agents/{agent.id}/edit",
                                       {"channel_action": "load"}, "/login").status_code, 200)
        self.assertIn("0000123", self.get(self.staff, "/tms/admin/").get_data(as_text=True))
        review = self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True)
        self.assertIn('data-channel-list=', review)
        self.assertIn('<option value="TMS_DEMO">', review)
        self.assertNotIn(TOKEN, review)
        pricing = self.pricing(agent_id=agent.id, agent_service="TMS_DEMO", surcharge="5.50", operation="6.25",
                               high_value="7.75", other_mode="items", other_name="虚构费用", other_amount="8.40",
                               other_currency="CNY", other_note="虚构说明")
        invalid = self.decision(order_id, 2, decision="approve", **(pricing | {"agent_service": "OTHER-ACCOUNT"}))
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("请选择此 NextSLS 代理当前已核验的渠道", invalid.get_data(as_text=True))
        self.assertEqual(self.decision(order_id, 2, decision="approve", **pricing).status_code, 302)
        self.carrier_post.assert_not_called()
        receivable = db.session.get(TmsReceivable, order_id)
        self.assertIsNone(receivable.total)
        self.assertEqual(receivable.details["extras_total"], "27.90")
        shipment = db.session.get(Shipment, receivable.shipment_id)
        self.assertIsNone(shipment.shipment_id)
        sent = []

        def fake(target, *, json, headers):
            self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
            if target.api_url.endswith("/get_services"):
                return success({"services": [SERVICE]})
            self.assertEqual(target.api_url, ORIGIN + "/api/v5/shipment/create")
            sent.append(json["shipment"])
            return success({"shipment": {"shipment_id": SUPPLIER_NUMBER}})

        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True), patch("carrier_http.post", side_effect=fake):
            self.assertEqual(self.action(order_id, "create").status_code, 400)
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 302)
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["service"], "TMS_DEMO")
        self.assertEqual(sent[0]["reference_1"], "0000123")
        self.assertEqual(sent[0]["reference_2"], "DEMO-PO")
        self.assertTrue(sent[0]["client_reference"].startswith("XR-"))
        self.assertEqual(sent[0]["to_address"]["postcode"], "00001")
        self.assertEqual(sent[0]["parcels"][0]["reference"], "0001")
        self.assertEqual(sent[0]["parcels"][0]["declarations"][0]["hscode"], "00123456")
        self.assertNotIn("photo_url", sent[0]["parcels"][0]["declarations"][0])
        self.assertNotIn("1227.90", str(sent))
        db.session.refresh(shipment)
        self.assertEqual((shipment.tracking_number, shipment.shipment_id, shipment.third_party_tracking_number),
                         ("0000123", SUPPLIER_NUMBER, None))
        self.assertEqual(Shipment.query.count(), 1)
        self.assertEqual(self.post(self.staff, f"/shipments/{shipment.id}/edit", {"shipment_id": "WRONG"}, "/login").status_code, 409)

    def test_tracking_dispatches_per_agent_without_legacy_url(self):
        self.staff_login()
        for index in (1, 2):
            result = self.post(self.staff, "/agents", {
                "name": f"自动轨迹测试 {index}", "provider": "nextsls", "base_url": f"https://demo{index}.nextsls.com",
                "app_token": f"fictional-tracking-token-{index}", "capability": "tracking",
            }, "/login")
            self.assertEqual(result.status_code, 302)
            agent = CarrierAgent.query.filter_by(name=f"自动轨迹测试 {index}").one()
            self.assertFalse(agent.api_url)
            db.session.add(Shipment(tracking_number=f"LOCAL-{index}", shipment_id=f"PROVIDER-{index}",
                                    agent_id=agent.id, customer_id=self.customer_a.id))
        db.session.commit()
        agent = CarrierAgent.query.filter_by(name="自动轨迹测试 1").one()
        url = f"/agents/{agent.id}/edit"
        body = self.get(self.staff, url).get_data(as_text=True)
        self.assertIn("POST https://demo1.nextsls.com/api/v5/shipment/get_tracking", body)
        self.assertIn("POST https://demo1.nextsls.com/api/v5/shipment/get_info", body)
        self.assertIn("系统未开放 NextSLS 只读请求", body)
        self.assertIn("未验证", body)
        self.assertNotIn("fictional-tracking-token-1", body)
        calls = []

        def fake(target, *, json, headers):
            number = json["shipment"]["shipment_id"]
            index = number[-1]
            self.assertEqual(target.api_url, f"https://demo{index}.nextsls.com/api/v5/shipment/get_tracking")
            self.assertEqual(headers["Authorization"], f"Bearer fictional-tracking-token-{index}")
            self.assertEqual(json, {"shipment": {"shipment_id": number, "language": "zh"}})
            calls.append(index)
            return success({"shipment": {"shipment_id": number, "traces": [{"info": "模拟轨迹到达"}]}})

        disabled = self.post(self.staff, "/track", {"numbers": "LOCAL-1"}, "/login")
        self.assertIn("系统未开放 NextSLS 只读请求", disabled.get_data(as_text=True))
        with patch.dict(app.config, TMS_NEXTSLS_READ_ENABLED=True), patch("carrier_http.post", side_effect=fake):
            result = self.post(self.staff, "/track", {"numbers": "LOCAL-1\nLOCAL-2"}, "/login")
            self.assertIn("模拟轨迹到达", result.get_data(as_text=True))
        self.assertEqual(calls, ["1", "2"])
        self.assertEqual(db.session.get(TmsAgentApi, agent.id).verified, {})

    def test_tracking_disable_and_tenant_change_do_not_reuse_legacy_credentials(self):
        order_id = self.approve()
        profile = self.profile()
        self.assign_supplier_number(order_id)
        url = f"/agents/{self.agent.id}/edit"
        settings = {"provider": "nextsls", "base_url": ORIGIN, "service_codes": "TMS_DEMO"}
        with patch.dict(app.config, TMS_NEXTSLS_READ_ENABLED=True):
            response = self.post(self.staff, "/track", {"numbers": "0000123"}, "/login")
            self.assertIn("功能未勾选启用", response.get_data(as_text=True))
        self.post(self.staff, url, settings | {"base_url": "https://other.nextsls.com", "api_url": ""}, "/login")
        self.assertEqual(profile.base_url, ORIGIN)
        self.assertEqual(self.agent.app_token, TOKEN)
        self.assertTrue(self.agent.api_url)
        self.post(self.staff, url, settings | {"capability": "tracking", "app_token": ""}, "/login")
        self.assertEqual(self.agent.app_token, TOKEN)
        with patch.dict(app.config, TMS_NEXTSLS_READ_ENABLED=True), patch("carrier_http.post", return_value=success(
                {"shipment": {"shipment_id": "OTHER", "traces": [{"info": "错误归属"}]}})):
            response = self.post(self.staff, "/track", {"numbers": "0000123"}, "/login")
            self.assertNotIn("错误归属", response.get_data(as_text=True))
            self.assertIn("归属或响应结构无法确认", response.get_data(as_text=True))

    def test_create_rejection_reports_safe_fields_and_locks_duplicate_attempt(self):
        order_id = self.approve()
        self.profile()
        rejection = SimpleNamespace(status_code=200, json=lambda: {
            "status": 0, "info": "photos is required; 缺少必填字段：brand；synthetic-private-message",
        })
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True), patch(
            "carrier_http.post", side_effect=[success({"services": [SERVICE]}), rejection],
        ) as transport:
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 302)
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
        self.assertEqual([call.args[0].api_url.split("/")[-1] for call in transport.call_args_list],
                         ["get_services", "create"])
        link = db.session.get(TmsSupplierOrder, order_id)
        self.assertEqual(link.state, "uncertain")
        self.assertFalse(link.supplier_number)
        attempt = TmsSupplierEvent.query.filter_by(order_id=order_id, kind="attempt").order_by(
            TmsSupplierEvent.id.desc()).first()
        self.assertTrue(attempt.details["supplier_rejected"])
        body = self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True)
        for label in ("代理返回失败 · 待对账", "商品图片（photos）", "品牌"):
            self.assertIn(label, body)
        self.assertNotIn("synthetic-private-message", body)
        self.assertNotIn("synthetic-private-message", str(attempt.details))

    def test_only_staff_can_correct_unsubmitted_supplier_address_placeholders(self):
        order_id = self.approve()
        self.profile()
        order = db.session.get(TmsOrder, order_id)
        original_file = order.workbook
        original_address = dict(order.invoice["address"], mobile="73", street2="73", street3="73")
        invoice = dict(order.invoice, address=original_address)
        invoice.pop("submitted_destination", None)
        invoice.pop("destination_review", None)
        order.invoice = invoice
        db.session.commit()
        revision = order.revision
        path = f"/tms/admin/orders/{order_id}/supplier"
        form = dict(revision=revision, address_confirmed="yes", address_reason="核对原始收件资料",
                    address_mobile="", address_street2="Verified Suite", address_street3="")
        self.assertIn('name="address_mobile"', self.get(self.staff, path.rsplit("/", 1)[0]).get_data(as_text=True))
        self.assertEqual(self.staff.post(path, data={"action": "address", **form}).status_code, 400)
        self.staff_login("tms-test-reader")
        self.assertEqual(self.action(order_id, "address", **form).status_code, 403)
        self.staff_login()
        self.assertEqual(self.action(order_id, "address", **(form | {"revision": revision - 1})).status_code, 400)
        self.assertEqual(self.action(order_id, "address", **(form | {"address_mobile": "73"})).status_code, 400)
        self.assertEqual(self.action(order_id, "address", **form, address_street="Must not change").status_code, 302)
        db.session.refresh(order)
        self.assertEqual(order.workbook, original_file)
        self.assertEqual(order.invoice["address"], original_address | {
            "mobile": "", "street2": "Verified Suite", "street3": "",
        })
        self.assertEqual(nextsls.unverified_destination_fields(order.invoice), [])
        self.assertEqual(order.invoice["supplier_address_reviews"][0]["before"], {
            "mobile": "73", "street2": "73", "street3": "73",
        })
        self.assertEqual(order.invoice["submitted_destination"]["address"], original_address)
        self.assertEqual(self.get(self.staff, path.rsplit("/", 1)[0]).status_code, 200)
        self.assertEqual(TmsAudit.query.filter_by(order_id=order_id, action="supplier_address").count(), 1)
        self.assign_supplier_number(order_id)
        order.invoice = dict(order.invoice, address=original_address)
        db.session.commit()
        self.assertEqual(self.action(order_id, "address", **(form | {"revision": order.revision})).status_code, 400)
        self.carrier_post.assert_not_called()

    def test_timeout_requires_reconciliation_never_blind_retry(self):
        order_id = self.approve()
        self.profile()
        paths = []
        matches = []
        photo_uploads = []

        def publish(mime, data):
            photo_uploads.append((mime, data))
            return "https://files.example.com/mock-photo.png"

        def fake(target, *, json, headers):
            path = target.api_url.split("/")[-1]
            paths.append(path)
            if path == "get_services":
                return success({"services": [SERVICE]})
            if path == "create":
                self.assertEqual(json["shipment"]["parcels"][0]["declarations"][0]["photo_url"],
                                 "https://files.example.com/mock-photo.png")
                raise carrier_http.CarrierRequestError("sensitive data never shown")
            self.assertEqual(path, "list")
            ref = json["shipment"]["client_reference"]
            return success({"shipment": [{"shipment_id": SUPPLIER_NUMBER, "client_reference": ref}] if matches else []})

        send_mock = partial(tms_carriers.send_order, transport=fake, photo_publisher=publish)
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True, TMS_NEXTSLS_READ_ENABLED=True), patch(
            "carrier_http.post", side_effect=fake,
        ), patch("tms_carriers.send_order", side_effect=send_mock):
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 302)
            self.assertEqual(db.session.get(TmsSupplierOrder, order_id).state, "uncertain")
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
            self.assertEqual(self.action(order_id, "prepare", revision=3).status_code, 400)
            self.assertEqual(self.action(order_id, "reconcile").status_code, 302)
            self.assertEqual(db.session.get(TmsSupplierOrder, order_id).state, "not_found")
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
            matches.append(True)
            self.assertEqual(self.action(order_id, "reconcile").status_code, 302)
        self.assertEqual(paths, ["get_services", "create", "list", "list"])
        self.assertEqual(photo_uploads, [("image/png", fixture.PNG)] * 3)
        self.assertEqual(db.session.get(TmsSupplierOrder, order_id).supplier_number, SUPPLIER_NUMBER)
        self.assertNotIn("sensitive data", self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True))

    def test_not_found_requires_explicit_manual_unlock_and_reason(self):
        order_id = self.approve()
        self.profile()
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True, TMS_NEXTSLS_READ_ENABLED=True), patch(
            "carrier_http.post", side_effect=[success({"services": [SERVICE]}),
                                              carrier_http.CarrierRequestError(), success({"shipment": []})],
        ) as request:
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 302)
            self.assertEqual(self.action(order_id, "reconcile").status_code, 302)
            self.assertEqual(self.action(order_id, "retry", reason="确认未建立").status_code, 400)
            self.assertEqual(self.action(order_id, "retry", confirm="yes", reason="").status_code, 400)
            self.assertEqual(self.action(order_id, "retry", confirm="yes", reason="确认未建立").status_code, 302)
            self.assertEqual(request.call_count, 3)
        self.assertEqual(db.session.get(TmsSupplierOrder, order_id).state, "new")
        self.assertEqual(TmsSupplierEvent.query.filter_by(kind="retry").one().details["reason"], "确认未建立")

    def test_channel_requirements_stop_create(self):
        order_id = self.approve()
        self.profile()
        required = SERVICE | {"packing_list_required": 1,
                              "require_declaration_list": ["amazon_ref_id", "brand", "model", "photos"]}
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True), patch(
            "carrier_http.post", return_value=success({"services": [required]}),
        ) as request:
            response = self.action(order_id, "create", confirm="yes")
            self.assertEqual(response.status_code, 400)
            self.assertIn("渠道要求装箱单", response.get_data(as_text=True))
            html = response.get_data(as_text=True)
            self.assertIn("photos", html)
            self.assertIn("Amazon 参考号、品牌、型号", html)
        self.assertEqual(request.call_count, 1)
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))

    def test_preparation_displays_only_channel_missing_fields_and_never_reuses_manual_attachment(self):
        required = SERVICE | {"packing_list_required": 1,
                              "require_declaration_list": ["brand", "model", "amazon_ref_id"]}
        profile = self.profile(rules={"TMS_DEMO": nextsls.preparation_rules(required)})
        order_id = self.approve()
        order = db.session.get(TmsOrder, order_id)
        order.invoice = dict(order.invoice, declarations=[
            dict(row, brand="Customer brand") for row in order.invoice["declarations"]
        ], nextsls_supplement={"agent_id": order.agent_id, "service": "TMS_DEMO",
            "packing_list": {"name": "old.xlsx", "url": "https://files.example.com/old.xlsx"}})
        db.session.commit()
        page = self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True)
        for message in ("系统未开放真实下单", "只读对账", "装箱单", "识别 3 / 3 张"):
            self.assertIn(message, page)
        self.assertNotIn('name="supplement_0_brand"', page)
        self.assertIn('name="supplement_0_amazon_ref_id"', page)
        self.assertIn('name="supplement_0_model"', page)
        self.assertNotIn('name="packing_url"', page)
        self.assertNotIn("files.example.com/old.xlsx", page)
        for invalid in ("无", "DEMO-PO", "0001", "0000123"):
            self.assertEqual(self.action(order_id, "prepare", revision=3,
                                         supplement_0_amazon_ref_id=invalid).status_code, 400)
        with self.assertRaisesRegex(nextsls.ProtocolError, "装箱单"):
            nextsls.create_payload(order.invoice, "TMS_DEMO", "XR-DEMO", required, {},
                                   supplement=tms_carriers.order_supplement(order), packing_host="files.example.com")

        tms_carriers.store_channel_catalogue(profile, {"TMS_DEMO": SERVICE["name"]}, {})
        db.session.commit()
        stale = self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True)
        self.assertIn("完整必填规则尚未读取", stale)
        self.assertNotIn('name="supplement_0_model"', stale)
        self.carrier_post.assert_not_called()

    def test_photo_required_channel_shows_separate_readiness_and_never_substitutes_photo_url(self):
        code = "B009-4"
        service = SERVICE | {"code": code, "require_declaration_list": ["amazon_ref_id", "photos"]}
        self.profile(service_codes=[code], rules={code: nextsls.preparation_rules(service)})
        order_id = self.pending_order()
        self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing(
            agent_service=code)).status_code, 302)
        path = f"/tms/admin/orders/{order_id}"
        html = self.get(self.staff, path).get_data(as_text=True)
        for label in ("A. 管理员可修正的资料和代理配置", "B. 待供应商确认的协议", "C. 单票授权及系统开关",
                      "识别 3 / 3 张", "photos 为本渠道必填，真实传输未配置", "供应商读取图片", "尚未验证",
                      "本票创建验收", "未提交", "本渠道无需单独装箱单", 'name="supplement_0_amazon_ref_id"'):
            self.assertIn(label, html)
        self.assertNotIn("渠道要求装箱单", html)
        self.carrier_post.assert_not_called()

        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True), patch(
            "carrier_http.post", return_value=success({"services": [service]}),
        ) as transport:
            result = self.action(order_id, "create", confirm="yes")
        self.assertEqual(result.status_code, 400)
        self.assertIn(nextsls.PHOTOS_PROTOCOL_BLOCKER, result.get_data(as_text=True))
        self.assertTrue(transport.call_args.args[0].api_url.endswith("/shipment/get_services"))
        self.assertEqual(transport.call_count, 1)
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))

    def test_bc_no_model_and_channel_change_drop_unneeded_amazon_field(self):
        profile = self.profile(service_codes=["TMS_DEMO", "OTHER"])
        profile.base_url = tms_carriers.BC_ORIGIN
        self.agent.api_url = ""
        both = SERVICE | {"require_declaration_list": ["amazon_ref_id", "model"]}
        model_only = SERVICE | {"require_declaration_list": ["model"]}
        tms_carriers.store_channel_catalogue(profile, {"TMS_DEMO": "原渠道", "OTHER": "新渠道"},
            {"TMS_DEMO": nextsls.preparation_rules(both), "OTHER": nextsls.preparation_rules(model_only)})
        db.session.commit()
        order_id = self.approve()
        fields = {f"supplement_{index}_{key}": value for index in range(3)
                  for key, value in (("amazon_ref_id", "FIXTURE-REF"), ("no_model", "yes"))}
        self.assertEqual(self.action(order_id, "prepare", revision=3, **fields).status_code, 302)
        self.assertEqual(self.action(order_id, "channel", revision=4, channel_code="OTHER").status_code, 302)
        db.session.expire_all()
        order = db.session.get(TmsOrder, order_id)
        payload = nextsls.create_payload(order.invoice, "OTHER", "XR-DEMO", model_only, {},
                                         supplement=tms_carriers.order_supplement(order))
        for box in payload["shipment"]["parcels"]:
            for row in box["declarations"]:
                self.assertEqual(row["model"], "无")
                self.assertNotIn("amazon_ref_id", row)
        page = self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True)
        self.assertNotIn('name="supplement_0_amazon_ref_id"', page)
        self.assertNotIn('name="supplement_0_model"', page)

    def test_b888_invoice_to_mock_create_uses_confirmed_supplement_and_one_reservation(self):
        rule = SERVICE | {"code": "B888", "name": "超级经济线", "packing_list_required": 1,
                          "require_declaration_list": ["amazon_ref_id", "name_zh", "name_en", "unit_value",
                                                       "qty", "material", "usage", "brand", "model", "hscode"]}
        self.profile(service_codes=["B888"], rules={"B888": nextsls.preparation_rules(rule)})
        self.sign_in(self.alice, "customer-a")
        raw = fixture.floating_workbook(service="超级经济线卡派")
        upload = self.upload(self.alice, raw)
        self.assertEqual(upload.status_code, 302)
        order_id = int(upload.location.rsplit("/", 1)[-1])
        self.assertEqual(self.post(self.alice, upload.location + "/confirm", {"revision": 1}).status_code, 302)
        self.staff_login()
        self.assertEqual(self.decision(order_id, 2, decision="approve",
                                       **self.pricing(agent_service="B888")).status_code, 302)
        attachment = "https://files.example.com/packing.xlsx?download=fixture"
        calls, requests, photos, drafts = [], [], [], []

        def publish(mime, data):
            photos.append((mime, data))
            return f"https://files.example.com/photo-{len(photos)}?download=fixture"

        def publish_packing(data):
            drafts.append(data)
            return attachment

        def mock(target, *, json, headers):
            path = target.api_url.rsplit("/", 1)[-1]
            calls.append(path)
            if path == "get_services":
                return success({"services": [rule]})
            self.assertEqual(path, "create")
            self.assertEqual(db.session.get(TmsSupplierOrder, order_id).state, "sending")
            requests.append(json["shipment"])
            return success({"shipment": {"shipment_id": SUPPLIER_NUMBER}})

        send_mock = partial(tms_carriers.send_order, transport=mock, photo_publisher=publish,
                            packing_publisher=publish_packing)
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True), patch(
            "tms_carriers.send_order", side_effect=send_mock,
        ):
            incomplete = self.action(order_id, "create", confirm="yes")
            self.assertIn("请逐条补充申报：Amazon 参考号、品牌、型号", incomplete.get_data(as_text=True))
            self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))
            fields = {f"supplement_{i}_{key}": f"fixture-{key}-{i}"
                      for i in range(3) for key in nextsls.SUPPLEMENTAL_DECLARATIONS}
            invalid = self.action(order_id, "prepare", revision=3, packing_name="packing.pdf",
                                  packing_url="http://127.0.0.1/private.pdf", **fields)
            self.assertEqual(invalid.status_code, 400)
            saved = self.action(order_id, "prepare", revision=3, **fields)
            self.assertEqual(saved.status_code, 302)
            response = self.action(order_id, "create", confirm="yes")
            self.assertIn("创建时的自动传输方式尚待供应商确认", response.get_data(as_text=True))
            self.assertNotIn(attachment, response.get_data(as_text=True))
            self.assertEqual(photos, [])
            self.assertEqual(drafts, [])
            with patch.dict(app.config, TMS_NEXTSLS_PACKING_ATTACHMENT_HOST="files.example.com"):
                self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 302)
                self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
        self.assertEqual(calls, ["get_services", "get_services", "get_services", "create"])
        self.assertEqual(len(requests), 1)
        payload = requests[0]
        link = db.session.get(TmsSupplierOrder, order_id)
        self.assertEqual((payload["service"], payload["reference_1"], payload["reference_2"], payload["parcel_count"]),
                         ("B888", "0000123", "DEMO-PO", "2"))
        self.assertEqual(payload["client_reference"], link.client_reference)
        self.assertEqual(payload["to_address"]["postcode"], "00001")
        self.assertNotIn("from_address", payload)
        self.assertEqual(payload["attachments"], [{"name": "packing.xlsx", "file_type": "order", "url": attachment}])
        self.assertEqual(len(drafts), 1)
        book = load_workbook(BytesIO(drafts[0]), read_only=True)
        try:
            self.assertEqual([sheet.max_row for sheet in book.worksheets], [3, 4])
        finally:
            book.close()
        self.assertEqual([len(box["declarations"]) for box in payload["parcels"]], [2, 1])
        declarations = [row for box in payload["parcels"] for row in box["declarations"]]
        self.assertEqual([row["photo_url"] for row in declarations],
                         [f"https://files.example.com/photo-{i}?download=fixture" for i in (1, 2, 3)])
        self.assertEqual(photos, list(zip(("image/png", "image/jpeg", "image/png"), fixture.FLOATING_PHOTOS)))
        self.assertFalse(any("photos" in row or "image_part" in row for row in declarations))
        declaration = payload["parcels"][1]["declarations"][0]
        self.assertEqual(declaration["amazon_ref_id"], fields["supplement_2_amazon_ref_id"])
        self.assertEqual(declaration["brand"], fields["supplement_2_brand"])
        self.assertEqual(declaration["model"], fields["supplement_2_model"])
        self.assertEqual((link.supplier_number, link.state), (SUPPLIER_NUMBER, "linked"))
        detail = self.get(self.alice, upload.location).get_data(as_text=True)
        for private in (SUPPLIER_NUMBER, attachment, "packing.xlsx", "fixture-brand", "files.example.com/photo"):
            self.assertNotIn(private, detail)
        self.assertEqual(db.session.get(TmsOrder, order_id).workbook, raw)

    def test_photo_mock_cannot_enable_real_bc_create(self):
        order_id = self.approve()
        profile = self.profile(service_codes=["B888"])
        profile.base_url = "https://bckjkj.nextsls.com"
        self.agent.api_url = profile.base_url + "/api/v5/shipment/get_tracking"
        order = db.session.get(TmsOrder, order_id)
        order.agent_service = "B888"
        db.session.commit()
        with patch.dict(app.config, ISOLATED_MODE=False, TMS_NEXTSLS_CREATE_ENABLED=True,
                        TMS_NEXTSLS_READ_ENABLED=True, TMS_NEXTSLS_CREATE_ORDER_ID=str(order_id)), patch(
            "carrier_http.post",
        ) as request:
            with self.assertRaisesRegex(nextsls.ProtocolError, "真实商品图片传输"):
                tms_carriers.send_order(order, 1)
            with self.assertRaisesRegex(nextsls.ProtocolError, "隔离模拟"):
                tms_carriers.send_order(order, 1, transport=request, photo_publisher=lambda *_: None)
            request.assert_not_called()
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))

    def test_expanded_b888_mock_maps_each_product_and_staff_packing_draft(self):
        rule = SERVICE | {"code": "B888", "packing_list_required": 1,
                          "require_declaration_list": ["amazon_ref_id", "brand", "model"]}
        self.profile(service_codes=["B888"], rules={"B888": nextsls.preparation_rules(rule)})
        self.sign_in(self.alice, "customer-a")
        raw = fixture.expanded_workbook()
        uploaded = self.upload(self.alice, raw)
        self.assertEqual(uploaded.status_code, 302)
        order_id = int(uploaded.location.rsplit("/", 1)[-1])
        self.assertEqual(self.post(self.alice, uploaded.location + "/confirm", {"revision": 1}).status_code, 302)
        self.staff_login()
        self.assertEqual(self.decision(order_id, 2, decision="approve",
                                       **self.pricing(agent_service="B888")).status_code, 302)
        fields = {f"supplement_{index}_{key}": f"{key}-{index}" for index in range(36)
                  for key in nextsls.SUPPLEMENTAL_DECLARATIONS}
        link = "https://files.example.com/packing.xlsx"
        self.assertEqual(self.action(order_id, "prepare", revision=3, **fields).status_code, 302)
        draft_path = f"/tms/admin/orders/{order_id}/supplier/packing-draft.xlsx"
        self.assertEqual(self.get(self.alice, draft_path).status_code, 302)
        response = self.get(self.staff, draft_path)
        self.assertEqual(response.status_code, 200)
        book = load_workbook(BytesIO(response.data), read_only=True)
        self.assertEqual([sheet.max_row for sheet in book.worksheets], [22, 37])
        self.assertEqual(sum(row[1] for row in book["货箱"].iter_rows(min_row=2, values_only=True)), 231)
        self.assertEqual(book["商品申报"]["G37"].value, "brand-35")
        book.close()
        photos = []
        requests = []

        def publish(mime, data):
            photos.append((mime, data))
            return f"https://files.example.com/p/{len(photos) - 1}"

        def transport(target, *, json, headers):
            if target.api_url.endswith("/get_services"):
                return success({"services": [rule]})
            self.assertTrue(target.api_url.endswith("/create"))
            requests.append(json["shipment"])
            return success({"shipment": {"shipment_id": SUPPLIER_NUMBER}})

        mock_send = partial(tms_carriers.send_order, transport=transport, photo_publisher=publish,
                            packing_publisher=lambda _: link)
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True,
                        TMS_NEXTSLS_PACKING_ATTACHMENT_HOST="files.example.com"), patch(
            "tms_carriers.send_order", side_effect=mock_send,
        ):
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 302)
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
        shipment, = requests
        self.assertEqual((shipment["reference_1"], shipment["reference_2"]), ("0000123", "DEMO-PO"))
        self.assertEqual(shipment["parcel_count"], "21")
        self.assertEqual(sum(box["client_weight"] for box in shipment["parcels"]), 231)
        self.assertEqual(photos, fixture.product_photos(raw, fixture.parse_invoice(raw)))
        for box in shipment["parcels"]:
            for product in box["declarations"]:
                index = int(product["name_en"].split("-")[1])
                self.assertEqual(box["reference"], f"{index % 21 + 1:04}")
                self.assertEqual(product["photo_url"], f"https://files.example.com/p/{index}")
                self.assertEqual((product["qty"], product["unit_value"]), (index + 1, index + 0.25))
                self.assertEqual([product[key] for key in nextsls.SUPPLEMENTAL_DECLARATIONS],
                                 [f"{key}-{index}" for key in nextsls.SUPPLEMENTAL_DECLARATIONS])

    def test_create_blocks_unreviewed_placeholder_on_historical_snapshot(self):
        invoice = fixture.parse_invoice(fixture.workbook_bytes())
        invoice["address"].update(street="73 Fictional Road", street2="73", mobile="73")
        with self.assertRaisesRegex(nextsls.ProtocolError, "占位值"):
            nextsls.create_payload(invoice, "TMS_DEMO", "XR-DEMO", SERVICE, {})
        self.assertEqual(invoice["address"]["mobile"], "73")
        invoice["address"]["mobile"] = ""
        with self.assertRaisesRegex(nextsls.ProtocolError, "占位值"):
            nextsls.create_payload(invoice, "TMS_DEMO", "XR-DEMO", SERVICE, {})
        invoice["destination_review"] = {"by": 1, "at": "fixture", "reason": "fixture",
                                         "verified_optional": {"street2": "73"}}
        payload = nextsls.create_payload(invoice, "TMS_DEMO", "XR-DEMO", SERVICE, {})
        self.assertEqual(payload["shipment"]["to_address"]["address_2"], "73")
        self.assertEqual(payload["shipment"]["to_address"]["address_1"], "73 Fictional Road")

    def test_photo_url_requires_verified_reference_and_cannot_replace_photos(self):
        invoice = fixture.parse_invoice(fixture.workbook_bytes())
        profile = SERVICE | {"require_declaration_list": ["photo_url"]}
        def payload(publisher=None):
            return nextsls.create_payload(invoice, "TMS_DEMO", "XR-DEMO", profile, {}, publish_photos=publisher)
        with self.assertRaisesRegex(nextsls.ProtocolError, "图片链接传输尚未获准"):
            payload()
        with self.assertRaisesRegex(nextsls.ProtocolError, "链接数量"):
            payload(lambda: ["https://files.example.com/image.png"])
        for unsafe in ("http://files.example.com/image.png", "https://a:b@files.example.com/image.png", "O23"):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(nextsls.ProtocolError, "HTTPS"):
                payload(lambda: [unsafe] * 3)
        profile["require_declaration_list"] = ["photos"]
        with self.assertRaisesRegex(nextsls.ProtocolError, "photos"):
            payload(lambda: self.fail("未确认 photos 协议时不能调用图片发布器"))

    def test_create_requires_configured_channel_and_live_account_visibility(self):
        order_id = self.approve()
        profile = self.profile(service_codes=[])
        path = f"/tms/admin/orders/{order_id}"
        body = self.get(self.staff, path).get_data(as_text=True)
        for label in ("待提交代理", "提交代理下单", "TMS_DEMO", "1 Fictional Way", "Demo item", "刷新 NextSLS 渠道清单"):
            self.assertIn(label, body)
        self.assertNotIn(TOKEN, body)
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True), patch(
            "carrier_http.post", return_value=success({"services": []}),
        ) as request:
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
            request.assert_not_called()
            tms_carriers.store_channel_catalogue(profile, {"TMS_DEMO": SERVICE["name"]})
            db.session.commit()
            self.assertEqual(self.action(order_id, "create", confirm="yes", revision=1).status_code, 400)
            request.assert_not_called()
            response = self.action(order_id, "create", confirm="yes")
            self.assertIn("所选渠道不在该账号的可用服务列表", response.get_data(as_text=True))
            self.assertEqual(request.call_count, 1)
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))

    def test_channel_editor_reads_and_saves_only_codes_visible_to_this_account(self):
        order_id = self.pending_order()
        profile = self.profile(service_codes=[])
        path = f"/agents/{self.agent.id}/edit"
        calls = []

        def mock_catalogue(target, *, json, headers):
            calls.append(target.api_url)
            self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
            self.assertEqual(json, {"services": {"type": "all"}})
            return success({"services": [{"code": "TMS_DEMO", "name": "虚构标准渠道"},
                                          {"code": "TMS_ALT", "name": "虚构其他渠道"}]})

        def edit(**values):
            return self.post(self.staff, path, values, "/login").get_data(as_text=True)

        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=False, TMS_NEXTSLS_READ_ENABLED=False), patch(
            "carrier_http.post", side_effect=mock_catalogue,
        ):
            html = edit(channel_action="load")
            self.assertIn("TMS_DEMO · 虚构标准渠道", html)
            self.assertNotIn(TOKEN, html)
            self.assertEqual(profile.service_codes, [])
            bad = edit(channel_action="save", sales_service="UPS红单", supplier_channel_code="WRONG")
            self.assertIn("该账号当前可用的渠道代码", bad)
            self.assertEqual(profile.service_codes, [])
            saved = edit(channel_action="save", sales_service="UPS红单", supplier_channel_code="TMS_DEMO")
            self.assertIn("推荐渠道已保存", saved)

        db.session.expire_all()
        self.assertEqual(set(tms_carriers.channel_catalogue(profile)), {"TMS_DEMO", "TMS_ALT"})
        self.assertNotIn("create", profile.verified)
        self.assertEqual(calls, [ORIGIN + "/api/v5/shipment/get_services"] * 3)
        self.assertEqual(db.session.get(TmsOrder, order_id).revision, 2)
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))
        self.assertEqual(self.post(self.staff, path, {"name": self.agent.name, "provider": "nextsls",
                         "base_url": ORIGIN, "capability": "create"}, "/login").status_code, 302)
        view = self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True)
        self.assertIn("TMS_DEMO（推荐）", view)
        response = self.decision(order_id, 2, decision="approve", **self.pricing(agent_service=""))
        self.assertEqual(response.status_code, 400)
        response = self.decision(order_id, 2, decision="approve", **self.pricing(agent_service="TMS_ALT"))
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.get(TmsOrder, order_id).agent_service, "TMS_ALT")
        self.assertEqual(self.staff.post(path, data={"channel_action": "load"}).status_code, 400)
        self.staff_login("tms-test-reader")
        self.assertEqual(self.post(self.staff, path, {"channel_action": "load"}, "/login").status_code, 403)

    def test_same_sales_service_can_select_distinct_channels_across_agents(self):
        order_id = self.pending_order()
        profile = self.profile()
        tms_carriers.store_channel_catalogue(profile, {"TMS_DEMO": "虚构海运", "ALT-1": "虚构卡派"})
        second_agent = CarrierAgent(name="虚构第二代理", app_token="fixture-second", supports_api=True)
        db.session.add(second_agent)
        db.session.flush()
        other = TmsAgentApi(agent_id=second_agent.id, provider="nextsls", base_url="https://second.nextsls.com",
                            enabled=["create"], service_codes=[], verified={}, sender={})
        tms_carriers.store_channel_catalogue(other, {"SECOND-KP": "另一家实际运输服务"})
        db.session.add(other)
        db.session.commit()
        html = self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True)
        self.assertIn("另一家实际运输服务 · SECOND-KP", html)
        self.assertIn("虚构卡派 · ALT-1", html)
        self.assertIn("搜索渠道名称或代码", html)
        for code in ("TMS_DEMO", "UPS红单", ""):
            invalid = self.decision(order_id, 2, decision="approve",
                                    **self.pricing(agent_id=second_agent.id, agent_service=code))
            self.assertEqual(invalid.status_code, 400)
        valid = self.decision(order_id, 2, decision="approve",
                              **self.pricing(agent_id=second_agent.id, agent_service="SECOND-KP"))
        self.assertEqual(valid.status_code, 302)
        order = db.session.get(TmsOrder, order_id)
        self.assertEqual((order.agent_id, order.agent_service, order.invoice["service"]),
                         (second_agent.id, "SECOND-KP", "UPS红单"))
        self.assertEqual(order.invoice["supplier_channel"], {"code": "SECOND-KP", "name": "另一家实际运输服务"})

    def test_one_xingrui_workbook_runs_through_three_supplier_accounts(self):
        self.staff_login()
        third = Customer(name="虚构第三客户")
        db.session.add(third)
        db.session.flush()
        db.session.add(TmsAccount(customer_id=third.id, username="customer-c",
                                  password_hash=fixture.generate_password_hash(self.password)))
        db.session.commit()
        third_client = app.test_client()
        raw = fixture.workbook_bytes()
        profiles = [
            ("https://first.nextsls.com", "A-OCEAN", "虚构海运", [], False),
            ("https://shared.nextsls.com", "B-TRUCK", "虚构卡派", ["brand", "model"], False),
            ("https://shared.nextsls.com", "C-FBA", "虚构仓配", ["amazon_ref_id"], True),
        ]
        order_refs, calls, sent = {}, {}, {}

        def transport(target, *, json, headers):
            index = {"Bearer fictional-a": 0, "Bearer fictional-b": 1,
                     "Bearer fictional-c": 2}[headers["Authorization"]]
            origin, code, name, required, packing = profiles[index]
            method = target.api_url.rsplit("/", 1)[-1]
            self.assertEqual(target.api_url, f"{origin}/api/v5/shipment/{method}")
            calls[index].append(method)
            if method == "get_services":
                return success({"services": [SERVICE | {"code": code, "name": name,
                    "require_declaration_list": required, "packing_list_required": int(packing)}]})
            request = json["shipment"]
            if method == "create":
                self.assertNotIn(index, sent)
                sent[index] = request
                order_refs[index] = request["client_reference"]
                if index == 2:
                    raise carrier_http.CarrierRequestError("fixture timeout")
                return success({"shipment": {"shipment_id": SUPPLIER_NUMBER}})
            if method == "list":
                self.assertEqual(request["client_reference"], order_refs[index])
                return success({"shipment": [{"shipment_id": SUPPLIER_NUMBER,
                                              "client_reference": order_refs[index]}]})
            self.assertEqual(request["shipment_id"], SUPPLIER_NUMBER)
            if method == "get_info":
                data = snapshot()
                data["shipment"]["client_reference"] = order_refs[index]
                return success(data)
            self.assertEqual(method, "get_tracking")
            return success({"shipment": {"shipment_id": SUPPLIER_NUMBER,
                                         "traces": [{"info": f"虚构轨迹-{index}"}]}})

        self.assertFalse(app.config["TMS_NEXTSLS_CREATE_ENABLED"])
        mock_send = partial(tms_carriers.send_order, transport=transport,
                            packing_publisher=lambda _: "https://files.example.com/pack.xlsx")
        with patch("carrier_http.post", side_effect=transport), patch("tms_carriers.send_order", side_effect=mock_send):
            for index, (client, suffix) in enumerate(zip((self.alice, self.bob, third_client), "abc")):
                origin, code, name, required, packing = profiles[index]
                calls[index] = []
                agent_name = f"虚构代理-{suffix}"
                settings = {"name": agent_name, "provider": "nextsls", "base_url": origin,
                            "app_token": f"fictional-{suffix}", "supports_api": "yes",
                            "capability": ["create", "reconcile", "tracking", "measurement", "charges"]}
                self.assertEqual(self.post(self.staff, "/agents", settings, "/login").status_code, 302)
                agent = CarrierAgent.query.filter_by(name=agent_name).one()
                editor = f"/agents/{agent.id}/edit"
                self.assertEqual(self.post(self.staff, editor, {"channel_action": "load"}, "/login").status_code, 200)
                page = self.get(self.staff, editor).get_data(as_text=True)
                self.assertIn(code, page)
                self.assertNotIn(f"fictional-{suffix}", page)
                self.assertEqual(tms_carriers.channel_catalogue(db.session.get(TmsAgentApi, agent.id)), {code: name})
                self.assertEqual(self.sign_in(client, f"customer-{suffix}").status_code, 302)
                uploaded = self.upload(client, raw)
                self.assertEqual(uploaded.status_code, 302)
                self.assertEqual(self.post(client, uploaded.location + "/confirm", {"revision": 1}).status_code, 302)
                order_id = int(uploaded.location.rsplit("/", 1)[-1])
                wrong_code = profiles[(index + 1) % 3][1]
                self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing(
                    agent_id=agent.id, agent_service=wrong_code)).status_code, 400)
                self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing(
                    agent_id=agent.id, agent_service=code)).status_code, 302)
                order = db.session.get(TmsOrder, order_id)
                self.assertEqual(order.invoice["supplier_tenant_origin"], origin)
                self.assertEqual(order.invoice["service"], "UPS红单")
                self.assertEqual(order.workbook, raw)
                self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
                self.assertEqual(calls[index], ["get_services"])

                with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True, TMS_NEXTSLS_READ_ENABLED=True,
                                TMS_NEXTSLS_PACKING_ATTACHMENT_HOST="files.example.com"):
                    if required:
                        missing = self.action(order_id, "create", confirm="yes")
                        self.assertEqual(missing.status_code, 400)
                        self.assertIn("请逐条补充申报", missing.get_data(as_text=True))
                        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))
                        fields = {f"supplement_{i}_{key}": f"verified-{key}-{i}"
                                  for i in range(3) for key in required}
                        self.assertEqual(self.action(order_id, "prepare", revision=3, **fields).status_code, 302)
                        db.session.expire_all()
                    created = self.action(order_id, "create", confirm="yes")
                    self.assertEqual(created.status_code, 302, re.findall(
                        r'<p class="error" role="alert">([^<]+)</p>', created.get_data(as_text=True)))
                    link = db.session.get(TmsSupplierOrder, order_id)
                    self.assertEqual(link.state, "uncertain" if index == 2 else "linked")
                    self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
                    if index == 2:
                        self.assertEqual(self.action(order_id, "reconcile").status_code, 302)
                        db.session.expire_all()
                    self.assertEqual((link.state, link.agent_id, link.supplier_number, link.tenant_origin),
                                     ("linked", agent.id, SUPPLIER_NUMBER, origin))
                    self.assertEqual(self.action(order_id, "sync").status_code, 302)
                    tracked = self.post(self.staff, "/track", {"numbers": "0000123",
                                        "customer_id": str(order.customer_id)}, "/login")
                    self.assertIn(f"虚构轨迹-{index}", tracked.get_data(as_text=True))

                payload = sent[index]
                self.assertEqual((payload["service"], payload["reference_1"], payload["reference_2"]),
                                 (code, "0000123", "DEMO-PO"))
                row = payload["parcels"][0]["declarations"][0]
                self.assertEqual({key: row[key] for key in nextsls.SUPPLEMENTAL_DECLARATIONS if key in row},
                                 {key: f"verified-{key}-0" for key in required})
                self.assertEqual("attachments" in payload, packing)
                self.assertEqual(calls[index].count("create"), 1)
        self.assertEqual(len(set(order_refs.values())), 3)
        self.assertFalse(app.config["TMS_NEXTSLS_CREATE_ENABLED"])

    def test_existing_supplier_binding_rejects_tenant_reassignment(self):
        order_id = self.approve()
        profile = self.profile(enabled=["create", "tracking", "measurement", "reconcile"])
        self.assign_supplier_number(order_id)
        with patch.dict(app.config, TMS_NEXTSLS_READ_ENABLED=True), patch(
            "carrier_http.post", return_value=success(snapshot()),
        ):
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
        link = db.session.get(TmsSupplierOrder, order_id)
        self.assertEqual(link.tenant_origin, ORIGIN)
        editor = f"/agents/{self.agent.id}/edit"
        result = self.post(self.staff, editor, {"provider": "nextsls", "base_url": "https://other.nextsls.com",
                           "clear_legacy_url": "yes", "app_token": "fictional-new-tenant-token",
                           "capability": ["tracking", "measurement"]}, "/login")
        self.assertEqual(result.status_code, 302)
        self.assertEqual(profile.base_url, ORIGIN)
        self.assertEqual(self.agent.app_token, TOKEN)
        self.assertIn("接入不同租户请新增代理", self.get(self.staff, editor).get_data(as_text=True))
        # Simulate older/manual configuration drift that predates the editor guard.
        profile.base_url = "https://other.nextsls.com"
        self.agent.api_url = ""
        db.session.commit()
        with patch.dict(app.config, TMS_NEXTSLS_READ_ENABLED=True):
            result = self.post(self.staff, "/track", {"numbers": "0000123"}, "/login")
            self.assertIn("与原订单关联不符", result.get_data(as_text=True))
            self.assertEqual(self.action(order_id, "sync").status_code, 400)

    def test_new_approval_cannot_create_using_reassigned_origin_with_same_channel(self):
        profile = self.profile()
        order_id = self.approve()
        order = db.session.get(TmsOrder, order_id)
        self.assertEqual(order.invoice["supplier_tenant_origin"], ORIGIN)
        profile.base_url = "https://other.nextsls.com"
        self.agent.api_url = ""
        db.session.commit()
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True):
            result = self.action(order_id, "create", confirm="yes")
        self.assertEqual(result.status_code, 400)
        self.assertIn("代理或租户已变化", result.get_data(as_text=True))
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))

    def test_expired_catalogue_and_token_changes_require_refresh(self):
        order_id = self.pending_order()
        profile = self.profile()
        old = dict(profile.verified[tms_carriers.CHANNEL_CATALOGUE],
                   fetched_at=(datetime.utcnow() - timedelta(hours=25)).isoformat())
        profile.verified = {tms_carriers.CHANNEL_CATALOGUE: old}
        db.session.commit()
        denied = self.decision(order_id, 2, decision="approve", **self.pricing(agent_service="TMS_DEMO"))
        self.assertEqual(denied.status_code, 400)
        self.assertIn("先刷新代理渠道清单", denied.get_data(as_text=True))
        tms_carriers.store_channel_catalogue(profile, {"TMS_DEMO": "虚构渠道"})
        db.session.commit()
        self.assertEqual(self.post(self.staff, f"/agents/{self.agent.id}/edit", {
            "provider": "nextsls", "base_url": ORIGIN, "capability": "create", "app_token": "fixture-replaced",
        }, "/login").status_code, 302)
        db.session.expire_all()
        self.assertFalse(tms_carriers.channel_catalogue(profile))

    def test_real_create_switch_also_requires_the_explicit_order_id(self):
        order_id = self.approve()
        self.profile()
        with patch.dict(app.config, ISOLATED_MODE=False, TMS_NEXTSLS_CREATE_ENABLED=True,
                        TMS_NEXTSLS_CREATE_ORDER_ID=str(order_id + 1)):
            response = self.action(order_id, "create", confirm="yes")
            self.assertEqual(response.status_code, 400)
            self.assertIn("此订单不在本次真实下单授权范围内", response.get_data(as_text=True))
        self.carrier_post.assert_not_called()

    def test_snapshot_billing_needs_matching_unit_and_changes_require_review(self):
        order_id = self.approve(unit="cbm")
        self.profile()
        self.assign_supplier_number(order_id)
        payload = snapshot()
        payload["shipment"]["chargeable_weight"] = "1.125"
        with patch.dict(app.config, TMS_NEXTSLS_READ_ENABLED=True), patch("carrier_http.post", return_value=success(payload)):
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
            def latest_id():
                return str(TmsSupplierEvent.query.filter_by(kind="snapshot").order_by(TmsSupplierEvent.id.desc()).first().id)
            def settle_snapshot():
                return self.settle(order_id, settlement_unit="cbm", settlement_source="snapshot",
                                   settlement_quantity="999", settlement_snapshot=latest_id())
            self.assertEqual(settle_snapshot().status_code, 400)
            self.assertEqual(self.action(order_id, "measurement_review", snapshot_id=latest_id(),
                                         unit_chargeable_weight="kg", reason="单位核实").status_code, 302)
            self.assertEqual(settle_snapshot().status_code, 400)
            self.assertEqual(self.action(order_id, "measurement_review", snapshot_id=latest_id(),
                                         unit_chargeable_weight="m³", reason="体积计费凭证").status_code, 302)
            self.assertEqual(settle_snapshot().status_code, 302)
            charge = db.session.get(TmsReceivable, order_id)
            self.assertEqual(str(charge.final_total), "13.50")
            self.assertEqual(charge.details["quantity"], "1.125")
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
            self.assertEqual(str(charge.final_total), "13.50")
            payload["shipment"]["chargeable_weight"] = "1.75"
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
            db.session.expire_all()
            self.assertIsNone(charge.final_total)
            self.assertEqual(str(charge.total), "13.50")
            self.assertIn("计费待复核", self.get(self.staff, "/finance").get_data(as_text=True))

    def test_unsubmitted_legacy_channel_can_be_corrected_without_sending(self):
        order_id = self.pending_order()
        self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing(
            agent_service="虚构销售服务")).status_code, 302)
        self.profile()
        form = {"revision": 3, "channel_code": "TMS_DEMO"}
        self.assertEqual(self.action(order_id, "channel", **(form | {"channel_code": "OTHER"})).status_code, 400)
        self.assertEqual(self.action(order_id, "channel", **form).status_code, 302)
        db.session.expire_all()
        order = db.session.get(TmsOrder, order_id)
        self.assertEqual(order.agent_service, "TMS_DEMO")
        self.assertEqual(order.invoice["supplier_channel_reviews"][0]["from"], "虚构销售服务")
        self.assertEqual(order.invoice["supplier_channel_reviews"][0]["to"], "TMS_DEMO")
        self.assertTrue(order.invoice["supplier_channel_reviews"][0]["by"])
        self.assertTrue(order.invoice["supplier_channel_reviews"][0]["at"])
        self.assertEqual(order.invoice["supplier_channel"]["name"], SERVICE["name"])
        self.assertEqual(db.session.get(TmsReceivable, order_id).agent_service, "TMS_DEMO")
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))
        self.carrier_post.assert_not_called()
        self.assertNotIn("TMS_DEMO", self.get(self.alice, f"/tms/customer/orders/{order_id}").get_data(as_text=True))
        self.assign_supplier_number(order_id)
        self.assertEqual(self.action(order_id, "channel", **(form | {"revision": 4})).status_code, 400)

    def assign_supplier_number(self, order_id):
        receivable = db.session.get(TmsReceivable, order_id)
        shipment = db.session.get(Shipment, receivable.shipment_id)
        shipment.shipment_id = SUPPLIER_NUMBER
        db.session.commit()
        return shipment

    def save_cost(self, order_id, **changes):
        snapshot_event = TmsSupplierEvent.query.filter_by(order_id=order_id, kind="snapshot").order_by(TmsSupplierEvent.id.desc()).first()
        cost = TmsSupplierEvent.query.filter_by(order_id=order_id, kind="cost").order_by(TmsSupplierEvent.id.desc()).first()
        values = {"currency": "USD", "payable": "13.20", "rate": "7", "rate_date": "2025-06-20",
                  "other": "10", "settlement": "pending", "cost_basis": "actual", "ownership": "payable",
                  "note": "fixture", "snapshot_id": str(snapshot_event.id) if snapshot_event else "",
                  "cost_id": str(cost.id) if cost else ""}
        return self.action(order_id, "cost", **(values | changes))

    def test_sync_cost_revision_and_internal_exports(self):
        order_id = self.approve()
        self.profile()
        receivable = db.session.get(TmsReceivable, order_id)
        shipment = db.session.get(Shipment, receivable.shipment_id)
        weight = shipment.weight
        shipment.shipment_id = SUPPLIER_NUMBER
        db.session.commit()
        with patch.dict(app.config, TMS_NEXTSLS_READ_ENABLED=True), patch(
            "carrier_http.post", side_effect=[success(snapshot()), success(snapshot()),
                                              success(snapshot(charge="15.2")), success(snapshot()),
                                              success(snapshot(number="WRONG"))],
        ):
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
            self.assertEqual(TmsSupplierEvent.query.filter_by(kind="snapshot").count(), 1)
            event = TmsSupplierEvent.query.filter_by(kind="snapshot").one()
            self.assertEqual(list(event.details["parcels"]), ["TEST-BOX"])
            self.assertNotIn("actual_weight", event.details["summary"])
            self.assertNotIn("client_weight", event.details["parcels"]["TEST-BOX"])
            self.assertEqual(shipment.weight, weight)
            self.assertNotIn("measurement", db.session.get(TmsAgentApi, self.agent.id).verified)
            self.assertEqual(self.save_cost(order_id).status_code, 302)
            pending_report = load_workbook(BytesIO(self.post(self.staff, "/finance", {"profit_export": "1"}, "/login").data))
            self.assertIsNone(pending_report.active["I2"].value)
            pending_report.close()
            self.assertEqual(self.settle(order_id).status_code, 302)
            response = self.post(self.staff, "/finance", {"profit_export": "1"}, "/login")
            report = load_workbook(BytesIO(response.data))
            self.assertEqual(report.active["I2"].value, 1097.6)
            report.close()
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
            self.assertIn("成本或币种待复核", self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True))
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
            self.assertEqual(self.action(order_id, "sync").status_code, 400)
        self.assertEqual(TmsSupplierEvent.query.filter_by(kind="snapshot").count(), 3)
        self.assertIn("成本或币种待复核", self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True))
        report = self.get(self.staff, f"/tms/admin/orders/{order_id}/supplier/measurements.xlsx")
        self.assertEqual(report.status_code, 200)
        book = load_workbook(BytesIO(report.data))
        self.assertEqual(book["逐箱计费明细"]["A2"].value, "TEST-BOX")
        book.close()
        customer_page = self.get(self.alice, f"/tms/customer/orders/{order_id}").get_data(as_text=True)
        self.assertNotIn("成本与利润", customer_page)
        self.assertNotIn("13.20", customer_page)
        self.staff_login("tms-test-reader")
        self.assertEqual(self.post(self.staff, "/finance", {"profit_export": "1"}, "/login").status_code, 403)
        self.assertNotIn("代理应付", self.get(self.staff, "/finance").get_data(as_text=True))


    def test_live_shape_measurements_are_audited_and_exports_are_role_scoped(self):
        order_id = self.approve()
        self.profile()
        shipment = self.assign_supplier_number(order_id)
        original_invoice = db.session.get(TmsOrder, order_id).invoice
        original_weight = shipment.weight
        db.session.add(ManualTrack(shipment_id=shipment.id, location=SUPPLIER_NUMBER,
                                   description="private supplier tracking note"))
        db.session.add(BankAccount(bank_name="Synthetic bank", account_name="Synthetic payee",
                                   account_no="TEST-ACCOUNT", is_default=True))
        db.session.commit()
        payload = snapshot()
        info = payload["shipment"]
        info.update(parcel_count=21, picked_count=21, actual_volume=123.45, volume_weight=0,
                    chargeable_weight=2.6, volume=2.55)
        info["parcels"] = [{"number": f"SUPPLIER-BOX-{index:02}", "actual_weight": "1.250",
                            "chargeable_weight": "1.4", "chargeable_length": "51",
                            "ext_number": "0001" if index == 1 else f"OTHER-{index:02}"}
                           for index in range(1, 22)]
        info["parcels"][1]["actual_weight"] = None
        with patch.dict(app.config, TMS_NEXTSLS_READ_ENABLED=True), patch("carrier_http.post", return_value=success(payload)):
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
        event = TmsSupplierEvent.query.filter_by(kind="snapshot").one()
        raw = event.details["raw"]
        self.assertEqual(raw["shipment"]["actual_volume"], 123.45)
        self.assertEqual(raw["parcels"]["SUPPLIER-BOX-01"]["actual_weight"], "1.250")
        self.assertEqual(event.details["parcels"]["SUPPLIER-BOX-01"]["actual_weight"], "1.25")
        self.assertEqual(len(event.details["parcels"]), 21)
        self.assertEqual(shipment.weight, original_weight)
        self.assertEqual(db.session.get(TmsOrder, order_id).invoice, original_invoice)

        staff_path = f"/tms/admin/orders/{order_id}"
        before = self.get(self.staff, staff_path).get_data(as_text=True)
        self.assertIn("逐箱测量（21 箱）", before)
        self.assertIn("供应商费用待确认", before)
        self.assertIn("零值含义待确认", before)
        self.assertNotIn("2.6 kg", before)
        self.assertEqual(self.action(order_id, "measurement_review", snapshot_id=str(event.id),
                                     unit_chargeable_weight="m³", unit_volume_weight="kg",
                                     unit_parcel_actual_weight="kg", reason="供应商计费单核对").status_code, 302)
        self.assertEqual(self.action(order_id, "measurement_review", snapshot_id=str(event.id),
                                     unit_chargeable_weight="kg", reason="过期提交").status_code, 400)

        internal = self.get(self.staff, staff_path + "/supplier/measurements.xlsx")
        self.assertEqual(internal.status_code, 200)
        book = load_workbook(BytesIO(internal.data), read_only=True)
        detail_rows = list(book["逐箱计费明细"].values)
        self.assertEqual(len(detail_rows), 22)
        self.assertEqual(detail_rows[1][:9], ("SUPPLIER-BOX-01", "0001", "0001", 1.5, 20, 10, 8, 1.25, "kg"))
        self.assertIsNone(detail_rows[2][7])
        self.assertIn(("计费体积", 2.6, "m³", "已确认", "chargeable_weight"), list(book["运单汇总"].values))
        book.close()

        customer_path = f"/tms/customer/orders/{order_id}"
        customer_page = self.get(self.alice, customer_path).get_data(as_text=True)
        self.assertIn("物流轨迹已更新", customer_page)
        self.assertIn("计费体积", customer_page)
        self.assertNotIn("private supplier tracking note", customer_page)
        self.assertNotIn(SUPPLIER_NUMBER, customer_page)
        self.assertNotIn(f"XR-{order_id}", customer_page)
        self.assertNotIn(f"XR-{order_id}", self.get(self.alice, "/tms/customer/").get_data(as_text=True))
        self.assertNotIn("成本与利润", customer_page)
        self.assertNotIn("123.45", customer_page)
        self.assertNotIn("材积重", customer_page)
        export = self.get(self.alice, customer_path + "/measurements.xlsx")
        self.assertEqual(export.status_code, 200)
        self.assertIn("no-store", export.headers["Cache-Control"])
        book = load_workbook(BytesIO(export.data), read_only=True)
        self.assertEqual(list(book["客户箱明细"].values)[1:], [("0001", "逐箱实重", 1.25, "kg")])
        text = str([list(sheet.values) for sheet in book.worksheets])
        for forbidden in (SUPPLIER_NUMBER, "SUPPLIER-BOX", "虚构代理", "13.20", "利润", "采购"):
            self.assertNotIn(forbidden, text)
        book.close()
        self.assertEqual(self.sign_in(self.bob, "customer-b").status_code, 302)
        self.assertEqual(self.get(self.bob, customer_path + "/measurements.xlsx").status_code, 404)
        self.assertEqual(self.get(self.alice, staff_path + "/supplier/measurements.xlsx").status_code, 302)

        self.assertEqual(self.settle(order_id).status_code, 302)
        response = self.post(self.staff, "/finance", {"export": "1"}, "/login")
        self.assertEqual(response.status_code, 200)
        book = load_workbook(BytesIO(response.data), read_only=True)
        self.assertNotIn(SUPPLIER_NUMBER, str([list(sheet.values) for sheet in book.worksheets]))
        book.close()

        info["chargeable_weight"] = 3.1
        with patch.dict(app.config, TMS_NEXTSLS_READ_ENABLED=True), patch("carrier_http.post", return_value=success(payload)):
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
        customer_page = self.get(self.alice, customer_path).get_data(as_text=True)
        self.assertNotIn("计费体积", customer_page)
        self.assertNotIn("3.1", customer_page)

        self.assertEqual(self.save_cost(order_id).status_code, 302)
        shipment.shipment_id = "DIFFERENT-FICTIONAL-SUPPLIER"
        db.session.commit()
        self.assertEqual(self.get(self.staff, staff_path + "/supplier/measurements.xlsx").status_code, 404)
        self.assertEqual(self.get(self.alice, customer_path + "/measurements.xlsx").status_code, 404)
        self.assertEqual(self.save_cost(order_id).status_code, 400)
        response = self.post(self.staff, "/finance", {"profit_export": "1"}, "/login")
        with BytesIO(response.data) as content:
            book = load_workbook(content)
            self.assertIsNone(book.active["I2"].value)
            self.assertEqual(book.active["P2"].value, "成本或币种待复核")
            book.close()

    def test_manual_supplier_link_rejects_different_customer_reference(self):
        order_id = self.approve()
        self.profile()
        self.assign_supplier_number(order_id)
        payload = snapshot()
        payload["shipment"]["client_reference"] = "SOMEONE-ELSE"
        with patch.dict(app.config, TMS_NEXTSLS_READ_ENABLED=True), patch("carrier_http.post", return_value=success(payload)):
            self.assertEqual(self.action(order_id, "sync").status_code, 400)
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))

    def test_cost_needs_ownership_basis_and_dated_fx_before_profit(self):
        order_id = self.approve()
        self.assertEqual(self.settle(order_id).status_code, 302)
        self.assertEqual(self.save_cost(order_id, rate_date="").status_code, 400)
        self.assertEqual(self.save_cost(order_id, rate_date="2999-01-01").status_code, 400)
        self.assertEqual(self.save_cost(order_id, cost_basis="estimated", settlement="settled").status_code, 400)
        self.assertEqual(self.save_cost(order_id, ownership="not_payable").status_code, 400)
        self.assertEqual(self.save_cost(order_id, cost_basis="estimated").status_code, 302)
        staff_path = f"/tms/admin/orders/{order_id}"
        view = self.get(self.staff, staff_path).get_data(as_text=True)
        self.assertIn("待确认实际成本", view)
        self.assertNotIn("1097.60", view)
        self.assertEqual(self.save_cost(order_id, cost_id="").status_code, 400)
        self.assertEqual(self.save_cost(order_id, settlement="settled").status_code, 302)
        costs = TmsSupplierEvent.query.filter_by(kind="cost").order_by(TmsSupplierEvent.id).all()
        self.assertEqual([event.details["cost_basis"] for event in costs], ["estimated", "actual"])
        self.assertTrue(all(event.actor_id and event.created_at for event in costs))
        self.assertEqual(costs[-1].details["rate_date"], "2025-06-20")
        self.assertIn("1097.60", self.get(self.staff, staff_path).get_data(as_text=True))


class NextSLSProtocolTests(unittest.TestCase):
    def test_invalid_response_is_not_a_zero_cost(self):
        for data in ({}, {"shipment": {"shipment_id": "WRONG"}}, snapshot(charge="NaN")):
            with self.subTest(data=data), self.assertRaises(nextsls.ProtocolError):
                nextsls.normalize_snapshot(data, SUPPLIER_NUMBER)

    def test_new_requests_refuse_a_different_tenant_and_unknown_path(self):
        agent = SimpleNamespace(is_active=True, supports_api=True, app_token=TOKEN,
                                api_url=ORIGIN + "/api/v5/shipment/get_tracking")
        with patch("carrier_http.post") as transport:
            with self.assertRaises(nextsls.ProtocolError):
                nextsls.request_data(agent, "https://other.nextsls.com", "/shipment/get_info", {})
            with self.assertRaises(nextsls.ProtocolError):
                nextsls.request_data(agent, ORIGIN, "/admin", {})
            transport.assert_not_called()

    def test_warehouse_code_is_distinct_from_address_lookup_key(self):
        def add_code(book):
            book["地址库"]["D2"] = "FBA-DEMO-WAREHOUSE"
        invoice = fixture.parse_invoice(fixture.workbook_bytes(change=add_code))
        self.assertEqual(invoice["address_code"], "DEMO-A")
        payload = nextsls.create_payload(invoice, "TMS_DEMO", "XR-DEMO", SERVICE, {})
        self.assertEqual(payload["shipment"]["to_warehouse_code"], "FBA-DEMO-WAREHOUSE")

    def test_weight_based_billing_is_labeled_only_after_unit_review(self):
        import tms_measurements
        details = nextsls.normalize_snapshot(snapshot(), SUPPLIER_NUMBER)
        details["identity_verified"] = True
        invoice = {"boxes": []}
        self.assertNotIn("计费重量", [row["label"] for row in tms_measurements.measurement_view(invoice, details)["summary"]])
        details["measurement_review"] = {"units": {"chargeable_weight": "kg"}}
        summary = tms_measurements.measurement_view(invoice, details)["summary"]
        self.assertIn(("计费重量", "kg"), [(row["label"], row["unit"]) for row in summary])

    def test_box_matching_ignores_internal_ordinals_and_conflicting_mappings(self):
        from tms_measurements import matched_boxes
        invoice = {"boxes": [{"box_number": code} for code in ("1", "C-02", "FBA000001")]}
        boxes = {"1": {"number": "1"},
                 "internal-2": {"ext_number": "C-02", "reference": "1"},
                 "internal-3": {"ext_number": "C-02"},
                 "internal-4": {"reference": "C-02"},
                 "FBA000001": {"number": "FBA000001"}}
        self.assertEqual(set(matched_boxes(invoice, boxes)), {"FBA000001"})

    def test_reconciliation_demands_exact_unique_reference(self):
        row = {"shipment_id": SUPPLIER_NUMBER, "client_reference": "XR-DEMO"}
        for matches in ([row, row], [row | {"client_reference": "other"}], {"shipment_id": SUPPLIER_NUMBER}):
            with self.assertRaises(nextsls.ProtocolError):
                nextsls.lookup_created({"shipment": matches}, "XR-DEMO")
