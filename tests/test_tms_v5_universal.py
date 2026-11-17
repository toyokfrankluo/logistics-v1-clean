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
from tms_models import TmsAgentApi, TmsOrder, TmsSupplierOrder, TmsSupplierEvent, TmsCreateControl, TmsReceivable


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

    def test_new_accounts_13_and_100_complete_onboarding_review_create_and_query_with_same_engine(self):
        from urllib.parse import urlsplit
        from io import BytesIO
        from zipfile import ZipFile
        self.staff_login()
        db.session.add(TmsCreateControl(id=1,enabled=False,revision=1,history=[]));db.session.commit()
        references=[]
        with patch.dict(app.config,TMS_NEXTSLS_CREATE_MODE="admin",TMS_NEXTSLS_CREATE_ENABLED=False,
                TMS_NEXTSLS_READ_ENABLED=True,TMS_NEXTSLS_REQUIRE_ORDER_GRANT=True,
                TMS_NEXTSLS_FILE_ORIGIN="https://files.example.com"):
            self.assertEqual(self.post(self.staff,"/tms/admin/settings/nextsls-create",
                {"revision":1,"enabled":"yes","confirmed":"yes"},"/login").status_code,302)
            for aid,legacy in ((13,False),(100,True)):
                with self.subTest(account_id=aid,legacy=legacy):
                    db.session.add(CarrierAgent(id=aid-1,name=f"Synthetic preexisting {aid-1}",is_active=True))
                    db.session.commit()
                    origin=f"https://new-{aid}.nextsls.com";token=f"synthetic-new-token-for-{aid}-only"
                    code=f"US-{aid}";number=f"SYNTHETIC-{aid}";name=f"Synthetic supplier {aid}"
                    service={"code":code,"name":"美国测试线"} if legacy else fixture.SERVICE | {
                        "code":code,"name":"美国测试线","require_declaration_list":["photos","model"],"packing_list_required":1}
                    captured={}
                    anonymous=app.test_client()
                    def read(url):
                        target=urlsplit(url)
                        with app.app_context():return anonymous.get(target.path+"?"+target.query)
                    def transport(target,*,json,headers):
                        method=target.api_url.rsplit("/",1)[-1]
                        self.assertEqual(target.api_url,origin+"/api/v5/shipment/"+method)
                        self.assertEqual(headers["Authorization"],"Bearer "+token)
                        if method=="get_services":return fixture.success({"services":[service]})
                        if method=="create":
                            captured.update(json["shipment"])
                            for parcel in captured["parcels"]:
                                for row in parcel["declarations"]:
                                    self.assertEqual(read(row["photos"]).data,fixture.fixture.PNG)
                            attachment=captured["attachments"][0]
                            self.assertEqual(attachment["file_type"],"order")
                            with ZipFile(BytesIO(read(attachment["url"]).data)) as book:self.assertIsNone(book.testzip())
                            return fixture.success({"shipment":{"shipment_id":number}})
                        self.assertEqual(method,"get_info")
                        data=fixture.snapshot(number=number)
                        data["shipment"]["client_reference"]=captured["client_reference"]
                        return fixture.success(data)
                    with patch("carrier_http.get_document",return_value="/shipment/create /shipment/get_services"),patch("carrier_http.post",side_effect=transport):
                        self.assertEqual(self.post(self.staff,"/agents",{"onboarding":"v5","name":name,
                            "base_url":origin,"app_token":token},"/login").status_code,302)
                    agent=CarrierAgent.query.filter_by(name=name).one();self.assertEqual(agent.id,aid)
                    self.sign_in(self.alice,"customer-a")
                    def products(book):
                        for row in (23,24,25):
                            book["星睿发票"][f"O{row}"]="无"
                            if legacy:book["星睿发票"][f"P{row}"]="无"
                    uploaded=self.upload(self.alice,fixture.fixture.workbook_bytes(
                        order_number=f"SYNTHETIC-ORDER-{aid}",product_details=True,change=products))
                    oid=int(uploaded.location.rsplit("/",1)[1])
                    self.assertEqual(self.post(self.alice,f"/tms/customer/orders/{oid}/confirm",{"revision":1}).status_code,302)
                    options=self.post(self.staff,f"/tms/admin/orders/{oid}/supplier-channels",{"agent_id":aid},"/login").json
                    self.assertEqual(options["catalogue"],{code:"美国测试线"})
                    self.assertEqual(self.decision(oid,2,decision="approve",**self.pricing(agent_id=aid,agent_service=code)).status_code,302)
                    order=db.session.get(TmsOrder,oid)
                    if not legacy:
                        self.assertEqual(self.action(oid,"prepare",revision=order.revision,
                            **{f"supplement_{i}_no_model":"yes" for i in range(3)}).status_code,302)
                        db.session.expire_all()
                    order=db.session.get(TmsOrder,oid)
                    grant={"unknown_rules_confirmed":"yes","unknown_rules_basis":"Isolated synthetic account acceptance"} if legacy else {}
                    self.assertEqual(self.action(oid,"grant_create",revision=order.revision,confirm="yes",**grant).status_code,302)
                    db.session.expire_all()
                    with patch("carrier_http.post",side_effect=transport) as http:
                        self.assertEqual(self.action(oid,"create",confirm="yes").status_code,302)
                        self.assertEqual(self.action(oid,"create",confirm="yes").status_code,400)
                        self.assertEqual(self.action(oid,"sync").status_code,302)
                    self.assertEqual(http.call_count,3)
                    references.append(captured["client_reference"])
                    self.assertEqual(db.session.get(TmsSupplierOrder,oid).supplier_number,number)
                    shipment,_=tms_carriers.approved_shipment(order)
                    self.assertEqual((shipment.shipment_id,shipment.agent_id),(number,aid))
                    self.assertTrue(tms_carriers.latest_event(oid,"snapshot").details["identity_verified"])
                    self.assertEqual(order.invoice["service"],"UPS红单")
            self.assertEqual(len(references),2)
            self.assertNotEqual(references[0],references[1])
            self.assertEqual(self.post(self.staff,"/tms/admin/settings/nextsls-create",{"revision":2,"enabled":"no"},"/login").status_code,302)

    def test_acceptance_sample_copies_preserve_original_images_and_never_grant_or_create(self):
        from zipfile import ZipFile
        from io import BytesIO
        import tms_acceptance
        self.profile();sid=self.approve();source=db.session.get(TmsOrder,sid);raw=source.workbook
        agent=CarrierAgent(id=13,name="Synthetic destination",is_active=True,supports_api=True,app_token="synthetic-other-token")
        profile=TmsAgentApi(agent_id=13,provider="nextsls",base_url="https://other.nextsls.com",enabled=["create"],service_codes=[],sender={},verified={})
        tms_carriers.store_channel_catalogue(profile,{"US":"美国普通测试线"},{"US":nextsls.preparation_rules(fixture.SERVICE)})
        db.session.add_all([agent,profile]);db.session.commit()
        result=tms_acceptance.prepare(sid,[13],"e"*32,"Explicit sample reuse for API acceptance")
        repeated=tms_acceptance.prepare(sid,[13],"e"*32,"Explicit sample reuse for API acceptance")
        self.assertEqual(result,repeated)
        copy=db.session.get(TmsOrder,result[0]["order_id"])
        self.assertEqual(copy.status,"pending")
        self.assertNotIn(tms_carriers.CREATE_GRANT,copy.invoice)
        self.assertIsNone(db.session.get(TmsSupplierOrder,copy.id))
        self.assertEqual(source.workbook,raw)
        self.assertEqual(copy.invoice["boxes"],source.invoice["boxes"])
        self.assertEqual(copy.invoice["declarations"],source.invoice["declarations"])
        with ZipFile(BytesIO(raw)) as old,ZipFile(BytesIO(copy.workbook)) as new:
            self.assertEqual(old.read("xl/media/demo.png"),new.read("xl/media/demo.png"))
        self.assertNotEqual(copy.file_sha256,source.file_sha256)
        self.carrier_post.assert_not_called()

    def test_existing_success_receipt_uses_read_only_own_account_and_checks_identity(self):
        import os,time
        from hashlib import sha256
        self.profile();oid=self.approve()
        with patch.dict(app.config,TMS_NEXTSLS_CREATE_ENABLED=True),patch("carrier_http.post",side_effect=[
            fixture.success({"services":[fixture.SERVICE]}),fixture.success({"shipment":{"shipment_id":"SYNTHETIC-EXISTING"}})]):
            self.assertEqual(self.action(oid,"create",confirm="yes").status_code,302)
        db.session.expire_all()
        link=db.session.get(TmsSupplierOrder,oid)
        order=db.session.get(TmsOrder,oid)
        order.invoice={**order.invoice,"order_archive":{"reason":"Synthetic history archive"}}
        db.session.commit()
        info={"shipment":{"shipment_id":link.supplier_number,"client_reference":link.client_reference,"service_code":"TMS_DEMO"}}
        listed={"shipment":[{"shipment_id":link.supplier_number,"client_reference":link.client_reference}]}
        secret="synthetic-read-only-proof"
        with patch.dict(os.environ,RENDER="true"),patch.dict(app.config,TMS_NEXTSLS_CREATE_ENABLED=False,
                TMS_AGENT_IMPORT_SCOPE="preflight",TMS_AGENT_IMPORT_KEY_SHA256=sha256(secret.encode()).hexdigest(),
                TMS_AGENT_IMPORT_KEY_EXPIRES=int(time.time())+600),patch("carrier_http.post",side_effect=[fixture.success(info),fixture.success(listed)]) as http:
            result=self.staff.post("/tms/operations/agents/verify-existing",json={"order_id":oid},
                headers={"Authorization":"Bearer "+secret})
        self.assertEqual(result.status_code,200)
        self.assertTrue(all(result.json[key] for key in ("number_matches","reference_matches","channel_matches","list_matches")))
        self.assertFalse(result.json["create_called"])
        self.assertEqual([call.args[0].api_url.rsplit("/",1)[-1] for call in http.call_args_list],["get_info","list"])
        self.assertTrue(all(call.kwargs["headers"]["Authorization"]=="Bearer "+fixture.TOKEN for call in http.call_args_list))
