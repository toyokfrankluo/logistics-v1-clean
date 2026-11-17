"""Site control tests never contact a carrier; only the existing simulated HTTP adapter is used."""
import os
import time
import unittest
from hashlib import sha256
from unittest.mock import patch

import test_tms_nextsls as fixture
from app import app
from models import db
from tms_models import TmsAgentImport, TmsCreateControl, TmsSupplierOrder
import tms_create_control


class CreateControlTests(unittest.TestCase):
    for name in ("setUp", "drop_tables", "post", "get", "staff_login", "profile", "approve", "action",
                 "pending_order", "get_order", "sign_in", "upload", "decision", "pricing"):
        locals()[name] = getattr(fixture.NextSLSTests, name)

    def control(self):
        row = TmsCreateControl(id=1, enabled=False, revision=1, history=[])
        db.session.add(row)
        db.session.commit()
        self.enterContext(patch.dict(app.config, TMS_NEXTSLS_CREATE_MODE="admin",
            TMS_NEXTSLS_CREATE_ENABLED=False, TMS_NEXTSLS_CREATE_EMERGENCY_STOP=False,
            TMS_NEXTSLS_REQUIRE_ORDER_GRANT=True, TMS_NEXTSLS_READ_ENABLED=False))
        return row

    def test_admin_site_control_preserves_per_order_grant_real_post_shape_and_duplicate_lock(self):
        control = self.control()
        self.profile()
        order_id = self.approve()
        path = "/tms/admin/settings/nextsls-create"
        self.assertFalse(tms_create_control.enabled())
        self.assertEqual(self.post(self.staff, path, {"revision":1, "enabled":"yes"}, "/login").status_code,400)
        self.assertEqual(self.post(self.staff, path, {"revision":1, "enabled":"yes", "confirmed":"yes",
            "reason":"Synthetic test only"}, "/login").status_code,302)
        self.assertTrue(tms_create_control.enabled())
        self.assertFalse(app.config["TMS_NEXTSLS_CREATE_ENABLED"])
        self.assertEqual(self.action(order_id,"create",confirm="yes").status_code,400)
        self.assertEqual(self.action(order_id,"grant_create",confirm="yes",revision=3).status_code,302)
        def transport(target, *, json, headers):
            self.assertEqual(headers["Authorization"],"Bearer " + fixture.TOKEN)
            if target.api_url.endswith("/get_services"):
                return fixture.success({"services":[fixture.SERVICE]})
            self.assertEqual(target.api_url, fixture.ORIGIN + "/api/v5/shipment/create")
            self.assertEqual(json["shipment"]["service"], "TMS_DEMO")
            return fixture.success({"shipment":{"shipment_id":fixture.SUPPLIER_NUMBER}})
        with patch("carrier_http.post",side_effect=transport) as http:
            self.assertEqual(self.action(order_id,"create",confirm="yes").status_code,302)
            self.assertEqual(self.action(order_id,"create",confirm="yes").status_code,400)
        self.assertEqual(http.call_count,2)
        self.assertEqual(db.session.get(TmsSupplierOrder,order_id).supplier_number,fixture.SUPPLIER_NUMBER)
        self.assertEqual(self.post(self.staff,path,{"revision":1,"enabled":"no"},"/login").status_code,409)
        self.assertEqual(self.post(self.staff,path,{"revision":2,"enabled":"no"},"/login").status_code,302)
        self.assertFalse(tms_create_control.enabled())
        self.assertEqual(len(control.history),2)

    def test_only_admin_with_csrf_can_change_control_and_emergency_cannot_be_overridden(self):
        self.control()
        path="/tms/admin/settings/nextsls-create"
        self.assertEqual(self.alice.get(path).status_code,302)
        self.staff_login("tms-test-reader")
        self.assertEqual(self.get(self.staff,path).status_code,403)
        self.assertEqual(self.post(self.staff,path,{"revision":1,"enabled":"yes","confirmed":"yes"},"/login").status_code,403)
        self.staff_login()
        with app.app_context():
            self.assertEqual(self.staff.post(path,data={"revision":1,"enabled":"yes","confirmed":"yes"}).status_code,400)
        with patch.dict(app.config,TMS_NEXTSLS_CREATE_EMERGENCY_STOP=True):
            self.assertEqual(self.post(self.staff,path,{"revision":1,"enabled":"yes","confirmed":"yes"},"/login").status_code,400)
            self.assertFalse(tms_create_control.enabled())

    def test_scoped_activation_checks_identity_backs_up_and_preserves_credentials(self):
        self.control()
        profile=self.profile()
        self.agent.is_active=False
        self.agent.supports_api=True
        self.agent.app_key="synthetic-key-preserved"
        db.session.commit()
        secret="synthetic-activation-key"
        headers={"Authorization":"Bearer "+secret}
        body={"agent_id":self.agent.id,"expected_origin":fixture.ORIGIN,"expected_name":self.agent.name,
              "reason":"Explicit administrator activation"}
        with patch.dict(os.environ,RENDER="true"),patch.dict(app.config,
            TMS_AGENT_IMPORT_KEY_SHA256=sha256(secret.encode()).hexdigest(),
            TMS_AGENT_IMPORT_KEY_EXPIRES=int(time.time())+600,
            TMS_AGENT_IMPORT_SCOPE=f"activate:{self.agent.id}"):
            path="/tms/operations/agents/activate"
            self.assertEqual(self.staff.post(path,headers=headers,json=body|{"agent_id":999}).status_code,404)
            self.assertEqual(self.staff.post(path,headers=headers,json=body|{"expected_origin":"https://wrong.nextsls.com"}).status_code,409)
            self.assertFalse(self.agent.is_active)
            for _ in range(2):
                self.assertEqual(self.staff.post(path,headers=headers,json=body).status_code,200)
            self.assertTrue(self.agent.is_active)
            self.assertEqual(self.agent.app_token,fixture.TOKEN)
            self.assertEqual(self.agent.app_key,"synthetic-key-preserved")
            self.assertEqual(profile.base_url,fixture.ORIGIN)
            self.assertEqual(TmsAgentImport.query.count(),1)
            self.assertFalse(TmsAgentImport.query.one().previous[0]["config"]["agent"]["is_active"])
            self.assertEqual(self.staff.post("/tms/operations/agents/create-control",headers=headers,json={"enabled":"yes"}).status_code,405)
        self.carrier_post.assert_not_called()
