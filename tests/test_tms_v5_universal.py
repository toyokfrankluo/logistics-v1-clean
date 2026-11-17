"""Unified V5 acceptance. Accounts and cargo are synthetic; carrier HTTP is always mocked."""
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import carrier_http
import nextsls
import test_tms_nextsls as fixture
import tms_carriers
import tms_supplier_images
from app import app
from models import CarrierAgent, db
from tms_models import TmsAgentApi, TmsOrder, TmsSupplierOrder, TmsSupplierEvent


class UniversalV5Tests(unittest.TestCase):
    for name in ("setUp", "drop_tables", "post", "get", "staff_login", "profile", "approve", "action",
                 "pending_order", "get_order", "sign_in", "upload", "decision", "pricing"):
        locals()[name] = getattr(fixture.NextSLSTests, name)

    def invoice(self):
        return fixture.fixture.parse_invoice(fixture.fixture.workbook_bytes())

    def test_shared_photos_encoder_uses_exact_https_url_for_any_v5_tenant(self):
        invoice=self.invoice()
        rule=fixture.SERVICE | {"require_declaration_list":["photos"],"packing_list_required":1}
        urls=[f"https://files.example.com/photo-{i}.png?s=synthetic-signature" for i in range(3)]
        for host in ("first", "second", "future-hundredth"):
            profile=SimpleNamespace(provider="nextsls",base_url=f"https://{host}.nextsls.com",verified={})
            encoder=tms_supplier_images.photos_encoder(profile,"ANY")
            self.assertIs(encoder,nextsls.product_photo_url)
            payload=nextsls.create_payload(invoice,"ANY","XR-PREFLIGHT",rule,{},
                image_fields={"photos"},photos_protocol_confirmed=True,
                publish_image_fields=lambda:[{"photos":encoder(url)} for url in urls],
                packing_host="files.example.com",publish_packing=lambda:"https://files.example.com/packing.xlsx")
            rows=[r for p in payload["shipment"]["parcels"] for r in p["declarations"]]
            self.assertEqual([r["photos"] for r in rows],urls)
            self.assertTrue(all("photo_url" not in r for r in rows))
            self.assertEqual(payload["shipment"]["attachments"][0]["file_type"],"order")
        for bad in ("http://files.example.com/p.png", '["https://files.example.com/p.png"]', "", "FILE-ID"):
            with self.assertRaises(nextsls.ProtocolError):
                nextsls.create_payload(invoice,"ANY","XR-PREFLIGHT",rule,{},image_fields={"photos"},
                    publish_image_fields=lambda:[{"photos":bad}]*3,packing_host="files.example.com",
                    publish_packing=lambda:"https://files.example.com/packing.xlsx")

    def test_legacy_and_partial_rules_remain_unknown_but_preview_validates_known_requirements(self):
        invoice=self.invoice()
        for row in invoice["declarations"]:row.update(brand="无",model="无",sku="REAL-SYNTHETIC-SKU")
        rule=nextsls.preparation_rules({"code":"LEGACY"})
        self.assertIsNone(rule["packing_list_required"])
        self.assertIsNone(rule["require_declaration_list"])
        self.assertEqual(len(rule["unknown_rules"]),9)
        self.assertEqual(nextsls.preparation_rules(rule),rule)
        payload=nextsls.create_payload(invoice,"LEGACY","XR-PREVIEW",rule,{})
        self.assertEqual(payload["shipment"]["parcels"][0]["declarations"][0]["brand"],"无")
        partial=nextsls.preparation_rules({"require_declaration_list":["sku"],"email_required":1})
        self.assertEqual(partial["email_required"],1)
        with self.assertRaisesRegex(nextsls.ProtocolError,"email"):
            nextsls.create_payload(invoice,"PARTIAL","XR-PREVIEW",partial,{})
        invoice["address"]["email"]="synthetic@example.com"
        del invoice["declarations"][0]["sku"]
        with self.assertRaisesRegex(nextsls.ProtocolError,"SKU"):
            nextsls.create_payload(invoice,"PARTIAL","XR-PREVIEW",partial,{})
        with self.assertRaises(nextsls.ProtocolError):nextsls.preparation_rules({"email_required":"maybe"})

    def test_unknown_rules_need_explicit_order_grant_and_timeout_locks_second_attempt(self):
        self.profile(rules={})
        oid=self.approve()
        with patch.dict(app.config,TMS_NEXTSLS_CREATE_ENABLED=True,TMS_NEXTSLS_REQUIRE_ORDER_GRANT=True):
            self.assertEqual(self.action(oid,"grant_create",confirm="yes",revision=3).status_code,400)
            self.assertEqual(self.action(oid,"grant_create",confirm="yes",revision=3,
                unknown_rules_confirmed="yes",unknown_rules_basis="Reviewed synthetic invoice and physical attributes").status_code,302)
            order=db.session.get(TmsOrder,oid)
            self.assertEqual(len(order.invoice["supplier_authorization_history"]),1)
            def transport(target,*,json,headers):
                self.assertEqual(headers["Authorization"],"Bearer "+fixture.TOKEN)
                if target.api_url.endswith("/get_services"):
                    return fixture.success({"services":[{"code":"TMS_DEMO","name":"Legacy"}]})
                self.assertEqual(target.api_url,fixture.ORIGIN+"/api/v5/shipment/create")
                raise carrier_http.CarrierRequestError("synthetic timeout")
            with patch("carrier_http.post",side_effect=transport) as http:
                self.assertEqual(self.action(oid,"create",confirm="yes").status_code,302)
                self.assertEqual(self.action(oid,"create",confirm="yes").status_code,400)
            self.assertEqual(http.call_count,2)
            self.assertEqual(db.session.get(TmsSupplierOrder,oid).state,"uncertain")
            event=TmsSupplierEvent.query.filter_by(kind="attempt").first()
            self.assertFalse(event.details["unknown_rule_attempt"]["compatibility_verified"])
            self.assertEqual(len(event.details["unknown_rule_attempt"]["fields"]),9)

    def test_fresh_unknown_rules_change_invalidates_grant_before_any_create(self):
        self.profile(rules={})
        oid=self.approve()
        with patch.dict(app.config,TMS_NEXTSLS_CREATE_ENABLED=True,TMS_NEXTSLS_REQUIRE_ORDER_GRANT=True):
            self.assertEqual(self.action(oid,"grant_create",confirm="yes",revision=3,
                unknown_rules_confirmed="yes",unknown_rules_basis="Synthetic review").status_code,302)
            row={"code":"TMS_DEMO","name":"Partial","email_required":1}
            with patch("carrier_http.post",return_value=fixture.success({"services":[row]})) as http:
                self.assertEqual(self.action(oid,"create",confirm="yes").status_code,400)
            self.assertEqual(http.call_count,1)
            self.assertIsNone(db.session.get(TmsSupplierOrder,oid))

    def test_quick_connect_three_inputs_reads_docs_and_own_token_without_overwriting_existing(self):
        self.staff_login()
        self.agent.app_token="preserved-existing-synthetic-token"
        db.session.commit()
        form={"onboarding":"v5","name":"Synthetic new V5 supplier", "base_url":"https://future.nextsls.com",
              "app_token":"new-synthetic-app-token-123456"}
        docs="NextSLS /shipment/create /shipment/get_services"
        with patch("carrier_http.get_document",return_value=docs) as documents, patch(
                "carrier_http.post",return_value=fixture.success({"services":[{"code":"LEGACY","name":"Legacy"}]})) as http:
            result=self.post(self.staff,"/agents",form,"/login")
        self.assertEqual(result.status_code,302)
        self.assertEqual(documents.call_count,2)
        self.assertEqual(http.call_count,1)
        self.assertEqual(http.call_args.args[0].api_url,"https://future.nextsls.com/api/v5/shipment/get_services")
        self.assertEqual(http.call_args.kwargs["headers"]["Authorization"],"Bearer "+form["app_token"])
        new=CarrierAgent.query.filter_by(name=form["name"]).one()
        profile=db.session.get(TmsAgentApi,new.id)
        self.assertNotEqual(new.id,self.agent.id)
        self.assertEqual(self.agent.app_token,"preserved-existing-synthetic-token")
        self.assertFalse(new.api_url)
        self.assertEqual(profile.provider,"nextsls")
        self.assertEqual(tms_carriers.channel_catalogue(profile),{"LEGACY":"Legacy"})
        self.assertTrue(tms_carriers.channel_rules(profile,"LEGACY")["unknown_rules"])
        self.assertFalse(profile.verified["onboarding"]["create_acceptance_verified"])
        with patch("carrier_http.post") as http:
            self.post(self.staff,"/agents",form,"/login")
            http.assert_not_called()
        self.assertEqual(CarrierAgent.query.count(),2)
        self.assertNotIn(form["app_token"],self.get(self.staff,"/agents").get_data(as_text=True))

    def test_rejection_extracts_safe_fields_and_only_explicit_url_rejection_is_protocol_evidence(self):
        info="photos is required; vat_number: required; private-customer-and-url-do-not-echo"
        self.assertEqual(nextsls.create_rejection_fields(info),["photos","vat_number"])
        self.assertNotIn("private-customer",nextsls.create_rejection_message(info))
        self.assertFalse(nextsls.photos_url_rejected("photos: URL expired"))
        self.assertTrue(nextsls.photos_url_rejected("photos: unsupported URL"))
        profile=SimpleNamespace(provider="nextsls",verified={"photos_protocols":{
            "ONE":{"encoding":"unsupported","evidence":"supplier response"}}})
        self.assertIsNone(tms_supplier_images.photos_encoder(profile,"ONE"))
        self.assertIs(tms_supplier_images.photos_encoder(profile,"OTHER"),nextsls.product_photo_url)

    def test_quick_connect_authentication_failure_saves_no_new_account(self):
        self.staff_login()
        with patch("carrier_http.get_document",side_effect=carrier_http.CarrierRequestError()), patch(
            "carrier_http.post",return_value=SimpleNamespace(status_code=200,json=lambda:{"status":0,"info":"private"})):
            response=self.post(self.staff,"/agents",{"onboarding":"v5","name":"Rejected synthetic supplier",
                "base_url":"https://future.nextsls.com","app_token":"synthetic-invalid-token-123456"},"/login")
        self.assertEqual(response.status_code,302)
        self.assertEqual(CarrierAgent.query.count(),1)
        self.assertEqual(TmsAgentApi.query.count(),0)
