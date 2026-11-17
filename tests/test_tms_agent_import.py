from hashlib import sha256
from io import BytesIO
import base64
import json
import os
import time
import unittest
from unittest.mock import patch

from openpyxl import Workbook
import test_tms_nextsls as fixture
from app import app
from models import CarrierAgent, db
from tms_models import TmsAgentApi, TmsAgentImport
import nextsls
import tms_agent_import as importer
import tms_carriers


def workbook(rows):
    book = Workbook()
    book.active.title = "代理填写"
    for index, (name, origin, token) in enumerate(rows, 6):
        code = origin.split("//")[1].split(".")[0]
        encoded = base64.b64encode(json.dumps({"app_code": code, "token": token}).encode()).decode()
        values = [999, name, origin, token, "待接入", "待核实", encoded, ""]
        for column, value in enumerate(values, 1):
            book.active.cell(index, column, value)
    data = BytesIO()
    book.save(data)
    return data.getvalue()


class AgentImportTests(unittest.TestCase):
    setUp = fixture.NextSLSTests.setUp
    drop_tables = fixture.NextSLSTests.drop_tables
    post = fixture.NextSLSTests.post
    get = fixture.NextSLSTests.get
    staff_login = fixture.NextSLSTests.staff_login

    def test_import_matches_tenant_preserves_inactive_and_backs_up_original_config(self):
        self.agent.api_url = fixture.ORIGIN + "/api/v5/shipment/get_tracking"
        self.agent.app_token = "old-fixture-token-12345678"
        self.agent.app_key = "keep-original-app-key"
        self.agent.is_active = False
        db.session.commit()
        rows = [(self.agent.name, fixture.ORIGIN, fixture.TOKEN),
                ("New real supplier fixture", "https://second.nextsls.com", "second-fixture-token-12345678")]
        raw = workbook(rows)
        self.staff_login()
        with patch("carrier_http.post", return_value=fixture.success({"services": [fixture.SERVICE]})) as transport:
            result = self.post(self.staff, "/tms/admin/agents/import", {
                "confirmed": "yes", "accounts": (BytesIO(raw), "agents.xlsx")}, "/login")
        self.assertEqual(result.status_code, 302)
        self.assertEqual(transport.call_count, 2)
        self.assertTrue(all(call.args[0].api_url.endswith("/shipment/get_services") for call in transport.call_args_list))
        db.session.expire_all()
        self.assertFalse(self.agent.is_active)
        self.assertEqual(self.agent.app_key, "keep-original-app-key")
        self.assertEqual(self.agent.app_token, fixture.TOKEN)
        profile = db.session.get(TmsAgentApi, self.agent.id)
        self.assertEqual(profile.base_url, fixture.ORIGIN)
        self.assertEqual(len(tms_carriers.channel_catalogue(profile)), 1)
        batch = TmsAgentImport.query.one()
        self.assertEqual(batch.previous[0]["config"]["agent"]["app_token"], "old-fixture-token-12345678")
        self.assertEqual(sum(row["created"] for row in batch.result), 1)
        with patch("carrier_http.post", return_value=fixture.success({"services": [fixture.SERVICE]})):
            importer.apply_accounts(importer.parse_accounts(raw), 1, sha256(raw).hexdigest())
        self.assertEqual(CarrierAgent.query.count(), 2)
        body = self.get(self.staff, result.location).get_data(as_text=True)
        self.assertNotIn(fixture.TOKEN, body)
        self.assertNotIn("old-fixture-token", body)

    def test_conflict_rolls_back_entire_batch(self):
        db.session.add(TmsAgentApi(agent_id=self.agent.id, provider="rtb56", base_url="", enabled=[],
                                  service_codes=[], verified={}, sender={}))
        db.session.commit()
        raw = workbook([("First new supplier", "https://first.nextsls.com", fixture.TOKEN),
                        (self.agent.name, fixture.ORIGIN, "second-fixture-token-12345678")])
        with patch("carrier_http.post", return_value=fixture.success({"services": [fixture.SERVICE]})):
            with self.assertRaises(nextsls.ProtocolError):
                importer.apply_accounts(importer.parse_accounts(raw), 1, "fixture")
        self.assertEqual(CarrierAgent.query.count(), 1)
        self.assertEqual(TmsAgentImport.query.count(), 0)

    def test_temporary_operation_is_closed_by_default_and_idempotent_when_authorized(self):
        endpoint = "/tms/operations/agents/import"
        secret = "synthetic-operation-credential"
        digest = sha256(secret.encode()).hexdigest()
        raw = workbook([("Fixture account", fixture.ORIGIN, fixture.TOKEN)])
        self.assertEqual(self.staff.post(endpoint).status_code, 404)
        with patch.dict(os.environ, RENDER="true"), patch.dict(app.config,
                TMS_AGENT_IMPORT_KEY_SHA256=digest, TMS_AGENT_IMPORT_KEY_EXPIRES=int(time.time()) + 600):
            self.assertEqual(self.staff.post(endpoint).status_code, 404)
            for index in range(2):
                with patch("carrier_http.post", return_value=fixture.success({"services": [fixture.SERVICE]})) as transport:
                    result = self.staff.post(endpoint, headers={"Authorization": "Bearer " + secret},
                                             data={"accounts": (BytesIO(raw), "fixture.xlsx")})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(transport.call_count, 1 if index == 0 else 0)
            self.assertEqual(TmsAgentImport.query.count(), 1)
            with patch.dict(app.config, TMS_NEXTSLS_CREATE_ENABLED=True):
                self.assertEqual(self.staff.post(endpoint, headers={"Authorization": "Bearer " + secret}).status_code, 404)

    def test_dynamic_extra_fields_require_real_values_and_use_actual_magnetic_flag(self):
        from tms_invoice import parse_invoice
        import test_tms_portal
        invoice = parse_invoice(test_tms_portal.workbook_bytes())
        rules = fixture.SERVICE | {"require_declaration_list": ["sku", "sale_url", "is_magnetic"]}
        with self.assertRaises(nextsls.PreparationError):
            nextsls.create_payload(invoice, "TEST", "TEST-REF", rules, {}, preflight=True)
        saved = {str(i): {"sku": f"REAL-FIXTURE-SKU-{i}", "sale_url": f"https://example.com/product/{i}"}
                 for i in range(len(invoice["declarations"]))}
        payload = nextsls.create_payload(invoice, "TEST", "TEST-REF", rules, {},
                                         supplement={"declarations": saved})
        declarations = [d for p in payload["shipment"]["parcels"] for d in p["declarations"]]
        self.assertTrue(all(d["is_magnetic"] == 0 for d in declarations))
        self.assertEqual(declarations[0]["sku"], saved["0"]["sku"])
        self.assertEqual(declarations[0]["sale_url"], saved["0"]["sale_url"])
        invoice["flags"]["magnet"] = True
        magnetic = nextsls.create_payload(invoice, "TEST", "TEST-REF", rules, {},
                                          supplement={"declarations": saved})
        self.assertTrue(all(row["is_magnetic"] == 1 for parcel in magnetic["shipment"]["parcels"]
                            for row in parcel["declarations"]))
        basic = nextsls.create_payload(invoice, "TEST", "TEST-REF", fixture.SERVICE, {})
        self.assertTrue(all("sku" not in row and "sale_url" not in row and "is_magnetic" not in row
                            for parcel in basic["shipment"]["parcels"] for row in parcel["declarations"]))
        with self.assertRaises(nextsls.ProtocolError):
            nextsls.supplemental_value(invoice, "sale_url", "fake placeholder")

    def test_deployed_preflight_constructs_payload_without_transport_or_order_creation(self):
        import test_tms_portal
        from tms_invoice import parse_invoice
        token = "synthetic-operation-credential"
        invoice = parse_invoice(test_tms_portal.workbook_bytes())
        invoice["customer_number"] = "XR-PREFLIGHT-DO-NOT-SHIP"
        profile = TmsAgentApi(agent_id=self.agent.id, provider="nextsls", base_url=fixture.ORIGIN,
                              enabled=["create"], service_codes=[], sender={}, verified={})
        tms_carriers.store_channel_catalogue(profile, {"TEST": "Fixture service"},
                                             {"TEST": nextsls.preparation_rules(fixture.SERVICE)})
        db.session.add(profile)
        db.session.commit()
        with patch.dict(os.environ, RENDER="true"), patch.dict(app.config,
                TMS_AGENT_IMPORT_KEY_SHA256=sha256(token.encode()).hexdigest(),
                TMS_AGENT_IMPORT_KEY_EXPIRES=int(time.time()) + 600):
            result = self.staff.post("/tms/operations/agents/preflight", json=invoice,
                                      headers={"Authorization": "Bearer " + token})
        self.assertEqual(result.status_code, 200)
        row = result.get_json()["results"][0]
        self.assertEqual(row["passed"], ["TEST"])
        self.assertEqual(row["sample"]["shipment"]["service"], "TEST")
        self.assertEqual(row["transport_calls"], 0)
        self.carrier_post.assert_not_called()
