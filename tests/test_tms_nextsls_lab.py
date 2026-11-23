from io import BytesIO
import unittest
from unittest.mock import patch

import test_tms_nextsls as fixture
import tms_carriers
from app import app
from models import CarrierAgent, db
from tms_models import TmsAgentApi, TmsNextslsTestPolicy, TmsOrder, TmsSupplierOrder

PATH = "/tms/admin/nextsls-lab"


class LabTests(unittest.TestCase):
    for alias in ("setUp", "drop_tables", "get", "post", "staff_login", "profile",
                  "decision", "pricing", "action"):
        locals()[alias] = getattr(fixture.NextSLSTests, alias)

    def send(self, **fields):
        return self.post(self.staff, PATH, fields, "/login")

    def sample(self, marked=True):
        def change(book):
            if marked:
                book["星睿发票"]["A1"] = "【测试样本｜非真实新货件】"
        return BytesIO(fixture.fixture.workbook_bytes(order_number="LAB-SAMPLE-1", change=change)), "sample.xlsx"

    def record_policy(self, scope):
        return self.send(action="policy", scope=scope, evidence="Synthetic platform terms v1",
                         fee_type="free", fees="isolated sample only", days="30",
                         no_pickup="yes", evidence_confirm="yes")

    def test_preview_without_external_create_or_local_order(self):
        self.staff_login()
        self.profile()
        self.assertEqual(self.get(self.staff, PATH).status_code, 200)
        body = self.send(action="preview", agent_id=self.agent.id, code="TMS_DEMO",
                         workbook=self.sample()).get_data(as_text=True)
        self.assertIn("3 张原图", body)
        self.assertNotIn(fixture.TOKEN, body)
        self.assertEqual(TmsOrder.query.count(), 0)
        self.carrier_post.assert_not_called()

    def test_reusable_policy_stage_grant_and_revoke_without_create(self):
        self.staff_login()
        self.profile()
        first = self.agent.id
        other = CarrierAgent(name="另一个联调账号", is_active=True, supports_api=True,
                             app_token="synthetic-other-account-key")
        db.session.add(other)
        db.session.flush()
        other_profile = TmsAgentApi(agent_id=other.id, provider="nextsls", base_url="https://other.nextsls.com",
                                     enabled=["create"], sender={}, service_codes=[], verified={})
        db.session.add(other_profile)
        tms_carriers.store_channel_catalogue(other_profile, {"OTHER": "另一个渠道"},
                                            {"OTHER": fixture.SERVICE})
        db.session.commit()
        self.assertEqual(self.record_policy(f"{first}:TMS_DEMO {other.id}:OTHER").status_code, 302)
        policy = TmsNextslsTestPolicy.query.one()
        self.assertEqual(len(policy.scope), 2)
        self.assertEqual(self.send(action="stage", agent_id=first, code="TMS_DEMO",
                                   policy_id=policy.id, workbook=self.sample()).status_code, 200)
        self.assertEqual(TmsOrder.query.count(), 0)
        self.assertEqual(self.send(action="stage", agent_id=first, code="TMS_DEMO",
                                   policy_id=policy.id, fees_confirm="yes", workbook=self.sample(False)).status_code, 200)
        self.assertEqual(TmsOrder.query.count(), 0)
        result = self.send(action="stage", agent_id=first, code="TMS_DEMO",
                           policy_id=policy.id, fees_confirm="yes", workbook=self.sample())
        self.assertEqual(result.status_code, 302)
        order = TmsOrder.query.one()
        oid = order.id
        self.assertEqual(order.status, "pending")
        self.assertTrue(order.invoice["workbook_test_sample"])
        self.assertEqual(order.invoice["nextsls_lab_case"]["policy_id"], policy.id)
        self.assertEqual(self.decision(oid, 1, decision="approve", **self.pricing(agent_service="TMS_DEMO")).status_code, 302)
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_MODE="environment", TMS_NEXTSLS_CREATE_ENABLED=True):
            self.assertEqual(self.action(oid, "grant_create", revision=2, confirm="yes").status_code, 302)
            order = db.session.get(TmsOrder, oid, populate_existing=True)
            profile = db.session.get(TmsAgentApi, first)
            approval = tms_carriers.sample_create_approval(order, profile, self.agent)
            self.assertIsNotNone(tms_carriers.current_create_grant(order, profile, approval))
            with patch("nextsls.request_data", side_effect=fixture.nextsls.ProtocolError("stopped at read boundary")) as wire:
                self.assertEqual(self.action(oid, "create", confirm="yes").status_code, 400)
                self.assertEqual(wire.call_args.args[2], "/shipment/get_services")
            self.assertEqual(self.send(action="revoke", policy_id=policy.id).status_code, 302)
            with patch("nextsls.request_data") as wire:
                self.assertEqual(self.action(oid, "create", confirm="yes").status_code, 400)
                wire.assert_not_called()
        self.assertIsNone(db.session.get(TmsSupplierOrder, oid))
        self.carrier_post.assert_not_called()

    def test_lab_records_allowlisted_create_and_query_receipts(self):
        self.staff_login()
        self.profile()
        self.assertEqual(self.record_policy(f"{self.agent.id}:TMS_DEMO").status_code, 302)
        policy = TmsNextslsTestPolicy.query.one()
        self.assertEqual(self.send(action="stage", agent_id=self.agent.id, code="TMS_DEMO",
                                   policy_id=policy.id, fees_confirm="yes", workbook=self.sample()).status_code, 302)
        oid = TmsOrder.query.one().id
        self.assertEqual(self.decision(oid, 1, decision="approve", **self.pricing(agent_service="TMS_DEMO")).status_code, 302)
        calls, reference = [], []
        def reply(target, *, json, headers):
            method = target.api_url.rsplit("/", 1)[-1]
            calls.append(method)
            if method == "get_services":
                return fixture.success({"services": [fixture.SERVICE]})
            if method == "create":
                reference.append(json["shipment"]["client_reference"])
                self.assertTrue(reference[0].startswith("XR-LAB-"))
            return fixture.success({"shipment": {"shipment_id": fixture.SUPPLIER_NUMBER,
                "service_code": "TMS_DEMO", "client_reference": reference[0],
                "private_field": "must-not-store-in-receipt"}})
        with patch.dict(app.config, TMS_NEXTSLS_CREATE_MODE="environment",
                        TMS_NEXTSLS_CREATE_ENABLED=True, TMS_NEXTSLS_READ_ENABLED=True):
            self.assertEqual(self.action(oid, "grant_create", revision=2, confirm="yes").status_code, 302)
            with patch("carrier_http.post", side_effect=reply):
                self.assertEqual(self.action(oid, "create", confirm="yes").status_code, 302)
                self.assertEqual(self.action(oid, "create", confirm="yes").status_code, 400)
                self.assertEqual(self.action(oid, "query").status_code, 302)
        self.assertEqual(calls, ["get_services", "create", "get_info"])
        event = tms_carriers.latest_event(oid, "attempt")
        self.assertEqual(event.details["lab_response"], {"http_status": 200, "api_status": 1,
                         "shipment_id": fixture.SUPPLIER_NUMBER})
        event = tms_carriers.latest_event(oid, "reconcile")
        self.assertTrue(event.details["channel_matches"])
        self.assertTrue(event.details["reference_matches"])
        page = self.get(self.staff, f"/tms/admin/orders/{oid}").get_data(as_text=True)
        self.assertIn("联调创建回执：HTTP 200", page)
        self.assertNotIn("must-not-store-in-receipt", page)
