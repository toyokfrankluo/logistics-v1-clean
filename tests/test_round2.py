import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict

import test_v1 as v1  # This module establishes the offline runtime before app import.
from models import db, Shipment, ManualTrack, Customer, CarrierAgent, BankAccount


SYNC_PATHS = (
    "/admin/sync-to-supabase", "/admin/sync-tracking-details",
    "/admin/refresh-tracking", "/shipments/1/refresh",
    "/admin/refresh-full-history/1", "/admin/auto-sync-single/1",
    "/admin/auto-sync-all-safe", "/admin/auto-sync-all", "/admin/auto-sync-recent",
)


class SafetyTests(unittest.TestCase):
    setUpClass = classmethod(v1.StabilityTests.setUpClass.__func__)
    setUp = v1.StabilityTests.setUp
    tearDown = v1.StabilityTests.tearDown
    login = v1.StabilityTests.login
    token = v1.StabilityTests.token
    post = v1.StabilityTests.post
    manual = v1.StabilityTests.manual

    def prepare_relations(self):
        self.manual()
        self.other = Shipment(tracking_number="FICTIONAL-KEEP", customer=self.customer,
                              agent=self.agent, fee=1)
        db.session.add(self.other)
        db.session.flush()
        db.session.add(ManualTrack(shipment_id=self.other.id, description="另一票虚构轨迹"))
        db.session.add(BankAccount(bank_name="虚构银行", account_name="虚构户名", account_no="FAKE-ONLY"))
        db.session.commit()
        self.sid = self.shipment.id

    def assert_references_retained(self):
        self.assertEqual(Customer.query.count(), 1)
        self.assertEqual(CarrierAgent.query.count(), 1)
        self.assertEqual(BankAccount.query.count(), 1)
        self.assertEqual(v1.backend.User.query.count(), 2)

    def test_sqlite_foreign_keys_enabled(self):
        self.assertEqual(db.session.execute(db.text("PRAGMA foreign_keys")).scalar(), 1)

    def test_delete_cascades_only_owned_tracks(self):
        self.prepare_relations()
        self.login()
        result = self.post(f"/shipments/delete/{self.sid}")
        self.assertEqual(result.status_code, 302)
        self.assertIsNone(db.session.get(Shipment, self.sid))
        self.assertEqual(ManualTrack.query.filter_by(shipment_id=self.sid).count(), 0)
        self.assertEqual(Shipment.query.count(), 1)
        self.assertEqual(ManualTrack.query.count(), 1)
        self.assert_references_retained()

    def test_legacy_delete_has_same_cascade(self):
        self.prepare_relations()
        self.login()
        self.assertEqual(self.post(f"/shipments/{self.sid}/delete").status_code, 302)
        self.assertEqual(Shipment.query.count(), 1)
        self.assertEqual(ManualTrack.query.filter_by(shipment_id=self.sid).count(), 0)
        self.assert_references_retained()

    def test_route_delete_rolls_back_after_child_delete(self):
        self.prepare_relations()
        self.login()
        deleted_children = []
        def child_deleted(mapper, connection, target):
            deleted_children.append(target.id)
        def fail_parent(mapper, connection, target):
            raise SQLAlchemyError("injected failure after child SQL")
        event.listen(ManualTrack, "after_delete", child_deleted)
        event.listen(Shipment, "before_delete", fail_parent)
        try:
            for path in (f"/shipments/delete/{self.sid}", f"/shipments/{self.sid}/delete"):
                self.assertEqual(self.post(path).status_code, 409)
                self.assertEqual(Shipment.query.count(), 2)
                self.assertEqual(ManualTrack.query.count(), 2)
            self.assertTrue(deleted_children)
        finally:
            event.remove(ManualTrack, "after_delete", child_deleted)
            event.remove(Shipment, "before_delete", fail_parent)

    def test_delete_missing_record_does_not_change_data(self):
        self.manual()
        self.login()
        self.assertEqual(self.post("/shipments/delete/999999").status_code, 404)
        self.assertEqual(Shipment.query.count(), 1)
        self.assertEqual(ManualTrack.query.count(), 1)

    def test_admin_single_delete_cascades(self):
        self.prepare_relations()
        self.login()
        self.assertEqual(self.post("/admin/shipment/delete/", data={"id": str(self.sid)}).status_code, 302)
        self.assertEqual(Shipment.query.count(), 1)
        self.assertEqual(ManualTrack.query.count(), 1)
        self.assert_references_retained()

    def test_admin_bulk_delete_all_or_nothing(self):
        self.prepare_relations()
        self.login()
        ids = [self.sid, self.other.id]
        def fail_parent(mapper, connection, target):
            if target.id == ids[1]:
                raise SQLAlchemyError("injected second record failure")
        event.listen(Shipment, "before_delete", fail_parent)
        data = MultiDict([("action", "delete")] + [("rowid", str(i)) for i in ids])
        try:
            self.assertEqual(self.post("/admin/shipment/action/", data=data).status_code, 302)
            self.assertEqual(Shipment.query.count(), 2)
            self.assertEqual(ManualTrack.query.count(), 2)
        finally:
            event.remove(Shipment, "before_delete", fail_parent)
        self.assertEqual(self.post("/admin/shipment/action/", data=data).status_code, 302)
        self.assertEqual(Shipment.query.count(), 0)
        self.assertEqual(ManualTrack.query.count(), 0)
        self.assert_references_retained()

    def test_admin_reference_records_cannot_be_deleted(self):
        self.prepare_relations()
        self.login()
        for model, ident in (("customer", self.customer.id), ("carrieragent", self.agent.id),
                             ("bankaccount", BankAccount.query.first().id), ("user", self.member.id)):
            with self.subTest(model=model):
                self.assertEqual(self.post(f"/admin/{model}/delete/", data={"id": str(ident)}).status_code, 302)
        self.assert_references_retained()

    def test_deactivate_agent_retains_history(self):
        self.login()
        self.assertEqual(self.post(f"/agents/{self.agent.id}/delete").status_code, 302)
        db.session.refresh(self.shipment)
        db.session.refresh(self.agent)
        self.assertFalse(self.agent.is_active)
        self.assertEqual(self.shipment.agent_id, self.agent.id)

    def test_deleted_tracking_not_returned_from_cache(self):
        self.manual()
        number = self.shipment.tracking_number
        lookup = lambda: self.client.post("/public_track", json={"order_id": number})
        self.assertIsNone(lookup().json["error"])
        self.login()
        self.post(f"/shipments/delete/{self.shipment.id}")
        self.assertTrue(lookup().json["error"])

    def test_login_requires_csrf(self):
        data = {"username": "fictional-admin", "password": "fictional-password"}
        self.assertEqual(self.client.post("/login", data=data).status_code, 400)
        self.assertEqual(self.client.post("/login", data=data, headers={"X-CSRFToken": "bad"}).status_code, 400)
        self.assertEqual(self.post("/login", data=data).status_code, 302)

    def test_missing_or_invalid_csrf_blocks_writes(self):
        self.manual()
        self.login()
        for path in (f"/shipments/delete/{self.shipment.id}", "/admin/shipment/delete/",
                     "/admin/shipment/action/", "/customers", "/agents", "/bank_accounts", *SYNC_PATHS):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path).status_code, 400)
                self.assertEqual(self.client.post(path, headers={"X-CSRFToken": "invalid"}).status_code, 400)
        self.assertEqual(Shipment.query.count(), 1)
        self.assertEqual(ManualTrack.query.count(), 1)

    def test_token_from_another_session_rejected(self):
        self.login()
        outsider = self.app.test_client()
        token = self.token(outsider)
        response = self.client.post(f"/shipments/delete/{self.shipment.id}", headers={"X-CSRFToken": token})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Shipment.query.count(), 1)

    def test_valid_form_token_creates_customer(self):
        self.login()
        response = self.client.post("/customers", data={"name": "虚构新增客户", "csrf_token": self.token()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Customer.query.count(), 2)

    def test_read_only_json_lookup_does_not_require_csrf(self):
        self.manual()
        result = self.client.post("/public_track", json={"order_id": "FICTIONAL-001"})
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json["error"])
        self.assertEqual(Shipment.query.count(), 1)
        self.assertEqual(ManualTrack.query.count(), 1)

    def test_get_cannot_sync_delete_or_logout(self):
        self.login()
        for path in (*SYNC_PATHS, "/logout", "/shipments/delete/1", "/shipments/1/delete", "/admin/shipment/delete/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 405)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_member_cannot_write_even_with_csrf(self):
        self.login(admin=False)
        paths = (*SYNC_PATHS, "/shipments/delete/1", "/shipments/1/delete", "/shipments/add",
                 "/shipments/edit/1", "/shipments/1/edit", "/shipments/import",
                 "/shipments/1/tracks", "/agents", "/agents/1/edit", "/agents/1/delete",
                 "/customers", "/bank_accounts", "/admin/user/new/", "/admin/shipment/action/")
        with patch("views.fetch_tracking_from_api") as fetch:
            for path in paths:
                with self.subTest(path=path):
                    self.assertEqual(self.post(path).status_code, 403)
            fetch.assert_not_called()
        self.assertEqual(Shipment.query.count(), 1)
        self.assertEqual(Customer.query.count(), 1)

    def test_anonymous_cannot_write(self):
        for path in (*SYNC_PATHS, "/shipments/delete/1", "/agents", "/bank_accounts"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path).status_code, 302)

    def test_admin_sync_post_uses_mock_only(self):
        self.login()
        self.agent.supports_api = True
        db.session.commit()
        with patch("views.fetch_tracking_from_api", return_value=([], None)) as fetch:
            response = self.post(f"/shipments/{self.shipment.id}/refresh")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(fetch.call_count, 1)

    def test_member_read_queries_and_exports_still_work(self):
        self.login(admin=False)
        for path in ("/shipments", "/track", "/finance", "/customers", "/shipments/1/tracks"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.post("/finance", data={"export": "1"}).status_code, 200)
        text = self.client.get("/shipments").get_data(as_text=True)
        self.assertNotIn('action="/admin/refresh-tracking"', text)

    def test_logout_requires_valid_post(self):
        self.login()
        self.assertEqual(self.client.post("/logout").status_code, 400)
        self.assertEqual(self.post("/logout").status_code, 302)
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_live_forms_render_tokens(self):
        from bs4 import BeautifulSoup
        self.login()
        for path in ("/login", "/shipments", "/shipments/add", "/shipments/edit/1",
                     "/shipments/1/edit", "/customers", "/agents", "/agents/1/edit",
                     "/bank_accounts", "/finance", "/track", "/invoice", "/public/track",
                     "/shipments/1/tracks", "/admin/shipment/", "/admin/shipment/new/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                soup = BeautifulSoup(response.data, "html.parser")
                for form in soup.find_all("form"):
                    if form.get("method", "get").lower() == "post":
                        self.assertIsNotNone(form.find("input", {"name": "csrf_token"}))

    def test_admin_create_and_edit_with_csrf(self):
        self.login()
        response = self.post("/admin/customer/new/", data={"name": "虚构后台客户"})
        self.assertEqual(response.status_code, 302)
        customer = Customer.query.filter_by(name="虚构后台客户").one()
        response = self.post(f"/admin/customer/edit/?id={customer.id}",
                             data={"name": "虚构后台改名", "bank_info": "虚构说明"})
        self.assertEqual(response.status_code, 302)
        db.session.refresh(customer)
        self.assertEqual(customer.name, "虚构后台改名")
        self.assertEqual(customer.bank_info, "虚构说明")

    def test_session_forms_are_not_cross_origin_readable(self):
        origin = {"Origin": "https://untrusted.invalid"}
        response = self.client.get("/login", headers=origin)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        public = self.client.post("/public_track", json={"order_id": "UNKNOWN"}, headers=origin)
        self.assertEqual(public.status_code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", public.headers)
        self.assertNotIn("Access-Control-Allow-Credentials", public.headers)

    def test_sync_links_are_real_post_forms(self):
        from bs4 import BeautifulSoup
        self.login()
        soup = BeautifulSoup(self.client.get("/shipments").data, "html.parser")
        for endpoint in ("/admin/refresh-tracking", "/admin/auto-sync-all-safe",
                         "/admin/auto-sync-all", "/admin/auto-sync-recent",
                         "/admin/auto-sync-single/1", "/admin/refresh-full-history/1",
                         "/shipments/1/refresh"):
            with self.subTest(endpoint=endpoint):
                form = soup.find("form", {"action": endpoint})
                self.assertIsNotNone(form)
                self.assertEqual(form.get("method").lower(), "post")
                self.assertIsNotNone(form.find("input", {"name": "csrf_token"}))
                self.assertIsNone(soup.find("a", {"href": endpoint}))

    def test_admin_single_delete_failure_is_atomic(self):
        self.prepare_relations()
        self.login()
        def fail(mapper, connection, target):
            raise SQLAlchemyError("injected failure")
        event.listen(Shipment, "before_delete", fail)
        try:
            self.assertEqual(self.post("/admin/shipment/delete/", data={"id": str(self.sid)}).status_code, 302)
            self.assertEqual(Shipment.query.count(), 2)
            self.assertEqual(ManualTrack.query.count(), 2)
        finally:
            event.remove(Shipment, "before_delete", fail)

    def test_delete_does_not_call_remote_services(self):
        self.manual()
        self.login()
        with patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network")) as network:
            self.assertEqual(self.post(f"/shipments/delete/{self.shipment.id}").status_code, 302)
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
