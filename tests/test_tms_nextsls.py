"""NextSLS fixtures use synthetic orders and an entirely simulated carrier transport."""
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

import carrier_http
import nextsls
import test_tms_portal as fixture
from app import app
from models import CarrierAgent, Shipment, db
from tms_models import TmsAgentApi, TmsOrder, TmsReceivable, TmsSupplierEvent, TmsSupplierOrder


ORIGIN = "https://demo.nextsls.com"
SUPPLIER_NUMBER = "TEST-SLS-1"
TOKEN = "synthetic-test-token"
SERVICE = {
    "code": "TMS_DEMO", "require_declaration_list": ["name_en", "unit_value", "qty"],
    "from_address": "no", "tel_required": 0, "email_required": 0, "company_required": 0,
    "city_required": 1, "vat_number_required": 0, "eori_required": 0, "packing_list_required": 0,
}


def success(data):
    return SimpleNamespace(status_code=200, json=lambda: {"status": 1, "data": data})


def snapshot(number=SUPPLIER_NUMBER, charge="13.20"):
    return {"shipment": {
        "shipment_id": number, "chargeable_weight": "12", "picked_count": 2,
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
    pending_order = fixture.PortalTests.pending_order

    def approve(self):
        order_id = self.pending_order()
        self.assertEqual(self.decision(order_id, 2, decision="approve", **self.pricing(
            agent_service="TMS_DEMO")).status_code, 302)
        return order_id

    def profile(self, **changes):
        agent = db.session.get(CarrierAgent, self.agent.id)
        agent.app_token = TOKEN
        agent.api_url = ORIGIN + "/api/v5/shipment/get_tracking"
        settings = dict(provider="nextsls", base_url=ORIGIN,
                        enabled=["create", "reconcile", "measurement", "parcels", "charges"],
                        service_codes=["TMS_DEMO"], sender={}, verified={})
        profile = TmsAgentApi(agent_id=agent.id, **(settings | changes))
        db.session.add(profile)
        db.session.commit()
        return profile

    def action(self, order_id, action, **form):
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
        self.post(self.staff, url, {"provider": "nextsls", "base_url": "https://127.0.0.1",
                                    "capability": "create"}, "/login")
        self.assertEqual(db.session.get(TmsAgentApi, self.agent.id).base_url, ORIGIN)

    def test_default_gate_confirmation_and_admin_permissions(self):
        order_id = self.approve()
        self.profile()
        self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))
        self.assertEqual(self.staff.post(f"/tms/admin/orders/{order_id}/supplier",
                                         data={"action": "create"}).status_code, 400)
        self.staff_login("tms-test-reader")
        self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 403)
        self.assertEqual(self.get(self.alice, f"/tms/admin/orders/{order_id}/supplier/measurements.xlsx").status_code, 302)

    def test_approved_excel_creates_once_and_keeps_distinct_identifiers(self):
        order_id = self.approve()
        self.profile()
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
        shipment = db.session.get(Shipment, db.session.get(TmsReceivable, order_id).shipment_id)
        self.assertEqual((shipment.tracking_number, shipment.shipment_id, shipment.third_party_tracking_number),
                         ("0000123", SUPPLIER_NUMBER, None))
        self.assertEqual(Shipment.query.count(), 1)
        self.assertEqual(self.post(self.staff, f"/shipments/{shipment.id}/edit", {"shipment_id": "WRONG"}, "/login").status_code, 409)

    def test_timeout_requires_reconciliation_never_blind_retry(self):
        order_id = self.approve()
        self.profile()
        paths = []
        matches = []

        def fake(target, *, json, headers):
            path = target.api_url.split("/")[-1]
            paths.append(path)
            if path == "get_services":
                return success({"services": [SERVICE]})
            if path == "create":
                raise carrier_http.CarrierRequestError("sensitive data never shown")
            self.assertEqual(path, "list")
            ref = json["shipment"]["client_reference"]
            return success({"shipment": [{"shipment_id": SUPPLIER_NUMBER, "client_reference": ref}] if matches else []})

        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True, TMS_NEXTSLS_READ_ENABLED=True), patch("carrier_http.post", side_effect=fake):
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 302)
            self.assertEqual(db.session.get(TmsSupplierOrder, order_id).state, "uncertain")
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
            self.assertEqual(self.action(order_id, "reconcile").status_code, 302)
            self.assertEqual(db.session.get(TmsSupplierOrder, order_id).state, "not_found")
            self.assertEqual(self.action(order_id, "create", confirm="yes").status_code, 400)
            matches.append(True)
            self.assertEqual(self.action(order_id, "reconcile").status_code, 302)
        self.assertEqual(paths, ["get_services", "create", "list", "list"])
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
        required = SERVICE | {"packing_list_required": 1}
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True), patch(
            "carrier_http.post", return_value=success({"services": [required]}),
        ) as request:
            response = self.action(order_id, "create", confirm="yes")
            self.assertEqual(response.status_code, 400)
            self.assertIn("额外装箱单", response.get_data(as_text=True))
        self.assertEqual(request.call_count, 1)
        self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))

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
                                              success(snapshot(charge="15.2")), success(snapshot(number="WRONG"))],
        ):
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
            self.assertEqual(TmsSupplierEvent.query.filter_by(kind="snapshot").count(), 1)
            event = TmsSupplierEvent.query.filter_by(kind="snapshot").one()
            self.assertEqual(list(event.details["parcels"]), ["TEST-BOX"])
            self.assertNotIn("actual_weight", event.details["summary"])
            self.assertNotIn("client_weight", event.details["parcels"]["TEST-BOX"])
            self.assertEqual(shipment.weight, weight)
            self.assertEqual(db.session.get(TmsAgentApi, self.agent.id).verified, {})
            self.assertEqual(self.action(order_id, "cost", currency="USD", payable="13.20", rate="7",
                                         other="10", settlement="pending", note="fixture").status_code, 302)
            response = self.post(self.staff, "/finance", {"profit_export": "1"}, "/login")
            report = load_workbook(BytesIO(response.data))
            self.assertEqual(report.active["I2"].value, 1097.6)
            report.close()
            self.assertEqual(self.action(order_id, "sync").status_code, 302)
            self.assertEqual(self.action(order_id, "sync").status_code, 400)
        self.assertEqual(TmsSupplierEvent.query.filter_by(kind="snapshot").count(), 2)
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
        invoice = fixture.parse_invoice(fixture.workbook_bytes(), fixture.workbook_bytes(change=add_code))
        self.assertEqual(invoice["address_code"], "DEMO-A")
        payload = nextsls.create_payload(invoice, "TMS_DEMO", "XR-DEMO", SERVICE, {})
        self.assertEqual(payload["shipment"]["to_warehouse_code"], "FBA-DEMO-WAREHOUSE")

    def test_reconciliation_demands_exact_unique_reference(self):
        row = {"shipment_id": SUPPLIER_NUMBER, "client_reference": "XR-DEMO"}
        for matches in ([row, row], [row | {"client_reference": "other"}], {"shipment_id": SUPPLIER_NUMBER}):
            with self.assertRaises(nextsls.ProtocolError):
                nextsls.lookup_created({"shipment": matches}, "XR-DEMO")
