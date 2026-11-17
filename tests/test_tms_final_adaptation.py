import json
import base64
import os
import time
from hashlib import sha256
from unittest.mock import patch
import unittest

import test_tms_nextsls as fixture
from app import app
from models import db
from tms_models import TmsOrder, TmsSupplierPreviewFile
from tms_invoice import parse_invoice
import tms_supplier_files as files
import nextsls


class FinalAdaptationTests(unittest.TestCase):
    setUp = fixture.NextSLSTests.setUp
    drop_tables = fixture.NextSLSTests.drop_tables
    post = fixture.NextSLSTests.post
    get = fixture.NextSLSTests.get
    staff_login = fixture.NextSLSTests.staff_login
    profile = fixture.NextSLSTests.profile
    pending_order = fixture.NextSLSTests.pending_order
    get_order = fixture.NextSLSTests.get_order
    sign_in = fixture.NextSLSTests.sign_in
    upload = fixture.NextSLSTests.upload
    decision = fixture.NextSLSTests.decision
    pricing = fixture.NextSLSTests.pricing
    action = fixture.NextSLSTests.action

    def test_product_weight_size_identifiers_and_two_tax_numbers_require_real_source(self):
        invoice = parse_invoice(fixture.fixture.workbook_bytes())
        rules = fixture.SERVICE | {"require_declaration_list": ["weight", "size", "asin", "fnsku"],
                                   "vat_number_required": 1, "eori_required": 1}
        self.assertEqual(nextsls.missing_supplements(invoice, {}, rules)[0], ["weight", "size", "asin", "fnsku"])
        self.assertEqual(set(nextsls.missing_taxes(invoice, rules)), {"vat_number", "eori"})
        for row in invoice["declarations"]:
            row.update(unit_weight_kg="0.125", product_size="1×2×3", asin="B0TEST0001", fnsku="X0TEST0001")
        invoice["metadata"]["vat_eori"] = "VAT:TEST-VAT; EORI:TEST-EORI"
        payload = nextsls.create_payload(invoice, "TEST", "XR-TEST", rules, {})["shipment"]
        self.assertEqual((payload["vat_number"], payload["eori"]), ("TEST-VAT", "TEST-EORI"))
        row = payload["parcels"][0]["declarations"][0]
        self.assertEqual((row["weight"], row["size"], row["asin"], row["fnsku"]),
                         (0.125, "1*2*3", "B0TEST0001", "X0TEST0001"))
        for field, value in [("weight", "0"), ("weight", "-1"), ("size", "1*0*3"), ("asin", "无")]:
            with self.assertRaises(nextsls.ProtocolError):nextsls.supplemental_value(invoice, field, value)
        self.carrier_post.assert_not_called()

    def test_admin_can_save_required_tax_and_declaration_data_once(self):
        rules = fixture.SERVICE | {"require_declaration_list": ["weight", "size", "asin", "fnsku"],
                                   "vat_number_required": 1, "eori_required": 1}
        self.profile(rules={"TMS_DEMO": nextsls.preparation_rules(rules)})
        order_id = self.pending_order()
        self.assertEqual(self.decision(order_id, 2, decision="approve",
                                      **self.pricing(agent_service="TMS_DEMO")).status_code, 302)
        page = self.get(self.staff, f"/tms/admin/orders/{order_id}").get_data(as_text=True)
        self.assertIn('name="tax_vat_number"', page)
        self.assertIn('name="tax_eori"', page)
        data = {f"supplement_{i}_{field}":value for i in range(3) for field,value in (
            ("weight","0.125"),("size","1*2*3"),("asin","B0TEST0001"),("fnsku","X0TEST0001"))}
        result = self.action(order_id, "prepare", revision=3, tax_vat_number="TEST-VAT", tax_eori="TEST-EORI", **data)
        self.assertEqual(result.status_code, 302)
        order = db.session.get(TmsOrder, order_id)
        self.assertEqual(order.invoice["nextsls_tax"], {"vat_number":"TEST-VAT", "eori":"TEST-EORI"})
        self.assertEqual(order.invoice["nextsls_supplement"]["declarations"]["0"]["weight"], "0.125")
        self.carrier_post.assert_not_called()

    def test_synthetic_published_files_are_anonymous_signed_agent_bound_and_do_not_create_order(self):
        secret = "synthetic-temporary-adaptation-credential"
        digest = sha256(secret.encode()).hexdigest()
        self.profile()
        invoice = parse_invoice(fixture.fixture.workbook_bytes())
        invoice["customer_number"] = "XR-PREFLIGHT-DO-NOT-SHIP"
        photo = base64.b64encode(fixture.fixture.PNG).decode()
        headers = {"Authorization":"Bearer " + secret}
        with patch.dict(os.environ, RENDER="true"), patch.dict(app.config,
                TMS_AGENT_IMPORT_KEY_SHA256=digest,TMS_AGENT_IMPORT_KEY_EXPIRES=int(time.time())+600,
                TMS_AGENT_IMPORT_SCOPE="preflight",TMS_NEXTSLS_FILE_ORIGIN="https://files.example.com"):
            self.assertEqual(self.staff.post("/tms/operations/agents/import",headers=headers).status_code,404)
            result = self.staff.post("/tms/operations/agents/files", headers=headers, json={
                "batch_id":"a"*32,"invoice":invoice,"pictures":[{"data":photo}]*3})
            self.assertEqual(result.status_code,200)
            manifest=result.get_json()["results"][0]["files"]
            for item in manifest:
                from urllib.parse import urlsplit
                url=urlsplit(item["url"])
                self.assertEqual(self.alice.get(url.path).status_code,404)
                response=self.alice.get(url.path+"?"+url.query)
                self.assertEqual(response.status_code,200)
                self.assertEqual(sha256(response.data).hexdigest(),item["sha256"])
                self.assertEqual(files.preview_reference(item["url"],self.agent.id,invoice,item["slot"]),item["url"])
                with self.assertRaises(ValueError):files.preview_reference(item["url"],self.agent.id+1,invoice,item["slot"])
            deliveries={str(self.agent.id):{item["slot"]:item["url"] for item in manifest}}
            check=self.staff.post("/tms/operations/agents/preflight",headers=headers,
                                 json={"invoice":invoice,"deliveries":deliveries})
            self.assertEqual(check.status_code,200)
            self.assertTrue(check.get_json()["results"][0]["files_published"])
            self.assertEqual(TmsOrder.query.count(),0)
            self.assertEqual(TmsSupplierPreviewFile.query.count(),4)
            self.assertEqual(self.staff.delete("/tms/operations/agents/files",headers=headers,
                            json={"batch_id":"a"*32}).get_json()["deleted"],4)
            self.assertEqual(TmsSupplierPreviewFile.query.count(),0)
        self.carrier_post.assert_not_called()
