"""Batch regressions exercise the real admin routes with network transport intercepted."""
import re
import unittest
from unittest.mock import patch

import test_tms_nextsls as fixture
from app import app
from models import CarrierAgent, db
from tms_models import TmsCreateBatchItem, TmsOrder, TmsSupplierOrder

PATH = "/tms/admin/nextsls-batches"


class BatchTests(unittest.TestCase):
    for name in ("setUp", "drop_tables", "post", "get", "staff_login", "profile", "approve",
                 "action", "pending_order", "get_order", "sign_in", "upload", "decision", "pricing"):
        locals()[name] = getattr(fixture.NextSLSTests, name)

    def prepared(self, *, grant=True):
        self.enterContext(patch.dict(app.config, TMS_NEXTSLS_CREATE_MODE="environment",
                                    TMS_NEXTSLS_CREATE_ENABLED=True, TMS_NEXTSLS_READ_ENABLED=True))
        self.profile()
        order_id = self.approve()
        if grant:
            response = self.action(order_id, "grant_create", revision=3, confirm="yes")
            self.assertEqual(response.status_code, 302)
        return order_id

    def preview(self):
        response = self.post(self.staff, PATH, {"action": "preview", "agents": self.agent.id}, "/login")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def confirm(self, html):
        preview = re.search(r'name="preview" value="([^"]+)"', html).group(1)
        return self.post(self.staff, PATH, {"action": "confirm", "preview": preview,
                                            "confirmed": "yes"}, "/login")

    def test_batch_confirm_and_item_are_idempotent_with_writeback_and_query(self):
        order_id = self.prepared()
        html = self.preview()
        result = self.confirm(html)
        self.assertEqual(result.status_code, 302)
        batch = result.location.split("?")[0]
        self.assertEqual(self.confirm(html).location, batch)
        self.assertEqual(self.get(self.staff, batch).status_code, 200)
        self.assertEqual(TmsCreateBatchItem.query.count(), 1)
        submissions = []

        def fake(target, *, json, headers):
            if target.api_url.endswith("/get_services"):
                return fixture.success({"services": [fixture.SERVICE]})
            if target.api_url.endswith("/create"):
                submissions.append(json["shipment"]["client_reference"])
            elif not target.api_url.endswith("/get_info"):
                raise AssertionError(target.api_url)
            return fixture.success({"shipment": {"shipment_id": fixture.SUPPLIER_NUMBER,
                "service_code": "TMS_DEMO", "client_reference": submissions[0]}})

        endpoint = f"{batch}/items/{order_id}"
        self.assertEqual(self.staff.post(endpoint).status_code, 400)
        with patch("carrier_http.post", side_effect=fake):
            for _ in range(2):
                response = self.post(self.staff, endpoint, {"action": "run"}, "/login")
                self.assertEqual(response.json, {"state": "queried"})
        self.assertEqual(len(submissions), 1)
        self.assertEqual(db.session.get(TmsSupplierOrder, order_id).supplier_number, fixture.SUPPLIER_NUMBER)

    def test_preview_rechecks_changed_revision_and_ignores_historical_copy(self):
        order_id = self.prepared()
        html = self.preview()
        order = db.session.get(TmsOrder, order_id)
        order.invoice = {**order.invoice, "acceptance_source": {"order_id": 99}}
        db.session.commit()
        changed = self.confirm(html)
        self.assertIn("历史接口验收副本", changed.get_data(as_text=True))
        self.assertNotIn('name="preview"', self.preview())
        self.assertEqual(TmsCreateBatchItem.query.count(), 0)

    def test_uncertain_first_order_does_not_block_next_or_send_again(self):
        first = self.prepared(grant=False)
        first_agent = self.agent.id
        self.agent = CarrierAgent(name="另一个模拟代理", is_active=True)
        db.session.add(self.agent)
        db.session.commit()
        second_origin = "https://batch-second.nextsls.com"
        self.profile(base_url=second_origin)
        self.agent.app_token = "second-synthetic-token"
        self.agent.api_url = second_origin + "/api/v5/shipment/get_tracking"
        db.session.commit()
        response = self.upload(self.alice, fixture.fixture.workbook_bytes(order_number="0000124"))
        self.assertEqual(response.status_code, 302)
        second = int(response.location.rsplit("/", 1)[1])
        self.assertEqual(self.post(self.alice, f"/tms/customer/orders/{second}/confirm", {"revision": 1}).status_code, 302)
        self.assertEqual(self.decision(second, 2, decision="approve", **self.pricing(agent_service="TMS_DEMO")).status_code, 302)
        response = self.post(self.staff, PATH, {"action": "preview",
            "agents": f"{first_agent},{self.agent.id}"}, "/login")
        batch = self.confirm(response.get_data(as_text=True)).location.split("?")[0]
        for order_id in (first, second):
            order = db.session.get(TmsOrder, order_id)
            grant = order.invoice["nextsls_create_grant"]
            self.assertEqual(grant["batch_id"], batch.rsplit("/", 1)[1])
            self.assertEqual(grant["revision"], order.revision)
            self.assertEqual(len(order.invoice["supplier_authorization_history"]), 1)
            self.assertIsNone(db.session.get(TmsSupplierOrder, order_id))
        submissions = []

        def fake(target, *, json, headers):
            if target.api_url.endswith("/get_services"):
                return fixture.success({"services": [fixture.SERVICE]})
            if target.api_url.endswith("/list"):
                return fixture.success({"shipments": []})
            if target.api_url.endswith("/create"):
                submissions.append(json["shipment"]["client_reference"])
                origin, token = ((fixture.ORIGIN, fixture.TOKEN) if len(submissions) == 1
                                 else (second_origin, "second-synthetic-token"))
                self.assertEqual(target.api_url, origin + "/api/v5/shipment/create")
                self.assertEqual(headers["Authorization"], "Bearer " + token)
                if len(submissions) == 1:
                    raise ValueError()
            return fixture.success({"shipment": {"shipment_id": fixture.SUPPLIER_NUMBER,
                "service_code": "TMS_DEMO", "client_reference": submissions[-1]}})

        with patch("carrier_http.post", side_effect=fake):
            for order_id, expected in [(first, "uncertain"), (second, "queried"), (first, "uncertain")]:
                response = self.post(self.staff, f"{batch}/items/{order_id}", {"action": "run"}, "/login")
                self.assertEqual(response.json, {"state": expected})
        self.assertEqual(len(submissions), 2)
