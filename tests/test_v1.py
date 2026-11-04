"""Run only with unittest discovery in tests/, never legacy API scripts."""
import os
os.environ["LOGISTICS_ISOLATED"] = "1"
os.environ["DATABASE_URL"] = "sqlite:////DO-NOT-OPEN-business-database.db"

import contextlib
import io
import re
from html import unescape
import socket
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import requests
from openpyxl import load_workbook

import app as backend
from models import db, Customer, CarrierAgent, Shipment, ManualTrack
from utils import export_invoice


class StabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = backend.app
        cls.app.config.update(TESTING=True)
        assert cls.app.config["ISOLATED_MODE"]
        uri = cls.app.config["SQLALCHEMY_DATABASE_URI"]
        assert "logistics-v1-" in uri and uri.endswith("/test.db")

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.admin = backend.User(username="fictional-admin", is_admin=True)
        self.admin.set_password("fictional-password")
        self.member = backend.User(username="fictional-member", is_admin=False)
        self.member.set_password("fictional-password")
        self.customer = Customer(name="虚构客户甲")
        self.agent = CarrierAgent(name="虚构手工代理", supports_api=False)
        db.session.add_all([self.admin, self.member, self.customer, self.agent])
        db.session.flush()
        self.shipment = Shipment(
            tracking_number="FICTIONAL-001", shipment_id="PROVIDER-FICTIONAL-001",
            customer_id=self.customer.id, agent_id=self.agent.id,
            origin="虚构起点", destination="虚构目的地", carrier_id="fictional",
            channel="空运", product_type="测试品", pieces=2,
            weight=2, unit_price=10, surcharge_extra=3, operation_fee=4,
            high_value_fee=5, fee=32, status="已录入",
            created_at=datetime(2026, 1, 2, 23, 59, 59, 999999),
        )
        db.session.add(self.shipment)
        db.session.commit()
        backend.CACHE.clear()
        from public_fixtures import configure_public
        configure_public(self.app)
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def token(self, client=None):
        client = client or self.client
        # Isolate Flask-WTF's request-local token cache when tests keep an outer
        # app context open for fixture database operations.
        with self.app.app_context():
            html = client.get("/login").get_data(as_text=True)
        return unescape(re.search(r'name="csrf_token" value="([^"]+)"', html).group(1))

    def post(self, path, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers.setdefault("X-CSRFToken", self.token())
        return self.client.post(path, headers=headers, **kwargs)

    def login(self, admin=True):
        return self.post("/login", data={
            "username": "fictional-admin" if admin else "fictional-member",
            "password": "fictional-password",
        })

    def manual(self):
        db.session.add(ManualTrack(shipment_id=self.shipment.id,
                                  description="虚构仓库已接收", location="虚构仓库"))
        db.session.commit()

    def test_database_is_disposable(self):
        self.assertNotEqual(Path(db.engine.url.database).parent, Path.cwd() / "instance")
        self.assertTrue(self.app.config["ISOLATED_MODE"])

    def test_http_network_denied(self):
        with self.assertRaisesRegex(RuntimeError, "network disabled"):
            requests.get("https://example.invalid")

    def test_tcp_network_denied(self):
        with socket.socket() as sock:
            with self.assertRaisesRegex(RuntimeError, "network disabled"):
                sock.connect(("127.0.0.1", 9))

    def test_browser_external_fetch_denied(self):
        self.assertIn("connect-src 'self'", self.client.get("/frontend").headers["Content-Security-Policy"])

    def test_anonymous_admin_denied(self):
        for path in ("/admin/", "/admin/user/", "/admin/customer/", "/admin/shipment/new/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 302)

    def test_member_admin_denied(self):
        self.login(admin=False)
        for path in ("/admin/", "/admin/user/", "/admin/customer/", "/admin/shipment/new/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)

    def test_admin_access(self):
        self.login()
        for path in ("/admin/", "/admin/user/", "/admin/customer/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_unknown_public_tracking_is_error(self):
        response = self.post("/public_track", json={"order_id": "FICTIONAL-UNKNOWN"})
        self.assertTrue(response.json["error"])
        self.assertEqual(response.json["tracks"], [])
        self.assertNotIn("2025-09-10", response.get_data(as_text=True))

    def test_public_manual_tracking(self):
        self.manual()
        response = self.post("/public_track", json={"order_id": self.shipment.tracking_number})
        self.assertIsNone(response.json["error"])
        self.assertIn("物流事件已记录，详细内容暂不公开", response.json["tracks"])

    def test_public_web_error_displayed(self):
        response = self.post("/public/track", data={"numbers": "FICTIONAL-UNKNOWN"})
        self.assertIn("无法查询该运单", response.get_data(as_text=True))

    def test_public_api_manual_tracking(self):
        self.manual()
        response = self.client.get(f"/api/track_by_agent/{self.agent.id}/{self.shipment.tracking_number}")
        self.assertEqual(response.json["data"][0]["details"][0]["track_description"], "物流事件已记录，详细内容暂不公开")

    def test_public_real_adapter_with_mock_only(self):
        self.agent.supports_api = True
        self.agent.api_url = "https://fictional.txfba.com/tracking"
        self.agent.app_key = "fictional-private-key"
        self.agent.app_token = "fictional-private-token"
        self.shipment.shipment_id = None  # RTB/TXFBA do not require NextSLS ID.
        db.session.commit()
        output = io.StringIO()
        with patch("app.requests.post") as post, contextlib.redirect_stdout(output):
            post.return_value.status_code = 200
            post.return_value.json.return_value = {"success": "1", "data": [{"details": [
                {"track_occur_date": "2026-01-02", "track_description": "虚构接口事件"}
            ]}]}
            result = self.post("/public_track", json={"order_id": self.shipment.tracking_number})
        self.assertIn("物流事件已记录，详细内容暂不公开", result.json["tracks"])
        self.assertEqual(post.call_count, 1)
        self.assertNotIn("fictional-private", output.getvalue())
        self.assertNotIn("FICTIONAL-001", output.getvalue())

    def test_nextsls_uses_provider_id(self):
        self.agent.supports_api = True
        self.agent.api_url = "https://fictional.nextsls.com/api"
        db.session.commit()
        with patch("app.requests.post") as post:
            post.return_value.json.return_value = {"status": 1}
            backend.call_gettrack(None, self.shipment.tracking_number, agent_id=self.agent.id)
            self.assertEqual(post.call_args.kwargs["json"]["shipment"]["shipment_id"], self.shipment.shipment_id)

    def test_no_default_admin_created(self):
        db.session.query(backend.User).delete()
        db.session.commit()
        with patch.dict(os.environ, {}, clear=True):
            backend.ensure_admin_user()
        self.assertEqual(backend.User.query.count(), 0)

    def test_explicit_admin_credentials_not_logged(self):
        db.session.query(backend.User).delete()
        db.session.commit()
        db.session.expunge_all()
        with patch.dict(os.environ, {"ADMIN_USER": "fictional-new", "ADMIN_PASS": "only-test-secret"}):
            with self.assertLogs(self.app.logger, level="INFO") as logs:
                backend.ensure_admin_user()
        self.assertEqual(backend.User.query.count(), 1)
        self.assertNotIn("only-test-secret", " ".join(logs.output))

    def test_debug_disabled(self):
        self.assertFalse(self.app.debug)
        self.assertNotEqual(self.app.secret_key, "change-this-secret-for-dev")

    def test_partial_edit_preserves_fields(self):
        self.login()
        response = self.post(f"/shipments/edit/{self.shipment.id}", data={"note": "新备注"})
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.shipment)
        self.assertEqual(self.shipment.origin, "虚构起点")
        self.assertEqual(self.shipment.carrier_id, "fictional")
        self.assertEqual(self.shipment.agent_id, self.agent.id)
        self.assertEqual(self.shipment.customer_id, self.customer.id)
        self.assertEqual(self.shipment.fee, 32)
        self.assertEqual(self.shipment.note, "新备注")

    def test_edit_recalculates_fee(self):
        self.login()
        self.post(f"/shipments/edit/{self.shipment.id}", data={"weight": "3"})
        db.session.refresh(self.shipment)
        self.assertEqual(self.shipment.fee, 42)

    def test_invalid_edit_no_partial_write(self):
        self.login()
        self.post(f"/shipments/edit/{self.shipment.id}", data={"origin": "不能保存", "weight": "nan"})
        db.session.refresh(self.shipment)
        self.assertEqual(self.shipment.origin, "虚构起点")
        self.assertEqual(self.shipment.weight, 2)

    def test_legacy_edit_provider_alias(self):
        self.login()
        response = self.post(f"/shipments/{self.shipment.id}/edit",
                                    data={"agent_tracking_number": "NEW-FICTIONAL"})
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.shipment)
        self.assertEqual(self.shipment.shipment_id, "NEW-FICTIONAL")
        self.assertEqual(self.shipment.origin, "虚构起点")

    def test_legacy_manual_fee_preserved(self):
        self.login()
        self.post(f"/shipments/{self.shipment.id}/edit", data={"weight": "3", "fee": "99.50"})
        db.session.refresh(self.shipment)
        self.assertEqual(self.shipment.fee, 99.5)

    def test_provider_number_display(self):
        self.login()
        self.assertIn("PROVIDER-FICTIONAL-001", self.client.get("/shipments").get_data(as_text=True))

    def test_finance_end_date_includes_whole_day(self):
        self.login()
        db.session.add(Shipment(tracking_number="FICTIONAL-NEXT-DAY", fee=100,
                                created_at=datetime(2026, 1, 3)))
        db.session.add(Shipment(tracking_number="FICTIONAL-PREV-DAY", fee=100,
                                created_at=datetime(2026, 1, 1, 23, 59, 59)))
        db.session.commit()
        response = self.post("/finance", data={"date_from": "2026-01-02", "date_to": "2026-01-02"})
        text = response.get_data(as_text=True)
        self.assertIn("FICTIONAL-001", text)
        self.assertNotIn("FICTIONAL-NEXT-DAY", text)
        self.assertNotIn("FICTIONAL-PREV-DAY", text)

    def test_invalid_finance_dates(self):
        self.login()
        for dates in ({"date_to": "bad"}, {"date_from": "2026-02-01", "date_to": "2026-01-01"}):
            self.assertEqual(self.post("/finance", data=dates).status_code, 400)

    def test_finance_download(self):
        self.login()
        response = self.post("/finance", data={"export": "1"})
        self.assertEqual(response.status_code, 200)
        wb = load_workbook(io.BytesIO(response.data))
        self.assertEqual(wb.active.cell(5, 4).value, "PROVIDER-FICTIONAL-001")
        self.assertEqual(wb.active.cell(5, 10).value, 32)

    def test_concurrent_exports_are_independent(self):
        def export(name):
            stream = export_invoice("虚构公司", name, [{"order_no": name, "fee": 1}], [], filename=None)
            return load_workbook(stream).active.cell(5, 3).value
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(export, ["虚构甲", "虚构乙"])), ["虚构甲", "虚构乙"])

    def test_export_default_never_writes_shared_file(self):
        # OpenPyXL may use private temporary XML files. Protect the shared output,
        # rather than forbidding its legitimate internal temporary-file writes.
        previous = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="logistics-export-test-") as directory:
            try:
                os.chdir(directory)
                sentinel = Path("账单.xlsx")
                sentinel.write_bytes(b"fictional existing export")
                stream = export_invoice("虚构公司", "虚构客户", [], [])
                self.assertEqual(sentinel.read_bytes(), b"fictional existing export")
                self.assertEqual(list(Path('.').glob('*.xlsx')), [sentinel])
                self.assertIsInstance(stream, io.BytesIO)
                self.assertEqual(load_workbook(stream).active.cell(3, 1).value, "客户名称: 虚构客户")
            finally:
                os.chdir(previous)

    def test_duplicate_tracking_edit_rejected(self):
        self.login()
        db.session.add(Shipment(tracking_number="FICTIONAL-SECOND"))
        db.session.commit()
        self.post(f"/shipments/edit/{self.shipment.id}", data={"tracking_number": "FICTIONAL-SECOND"})
        db.session.refresh(self.shipment)
        self.assertEqual(self.shipment.tracking_number, "FICTIONAL-001")

    def test_existing_login_and_management_pages(self):
        self.assertEqual(self.login().status_code, 302)
        for path in ("/shipments", "/shipments/add", "/customers", "/agents", "/finance", "/bank_accounts", "/track"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_duplicate_route_winners(self):
        adapter = self.app.url_map.bind("localhost")
        rules = {"/agents": "agents", "/agents/1/edit": "edit_agent",
                 "/agents/1/delete": "delete_agent", "/shipments": "shipments",
                 "/shipments/add": "add_shipment", "/customers": "customers",
                 "/finance": "finance", "/bank_accounts": "bank_accounts",
                 "/shipments/1/tracks": "manual_tracks", "/track": "track", "/logout": "logout"}
        for path, endpoint in rules.items():
            with self.subTest(path=path):
                method = "POST" if endpoint in {"delete_agent", "logout"} else "GET"
                self.assertEqual(adapter.match(path, method=method)[0], "views." + endpoint)

    def test_agent_secrets_not_rendered(self):
        self.agent.app_token = "fictional-sensitive-token"
        self.agent.app_key = "fictional-sensitive-key"
        db.session.commit()
        self.login()
        for path in ("/agents", f"/agents/{self.agent.id}/edit"):
            text = self.client.get(path).get_data(as_text=True)
            self.assertNotIn("fictional-sensitive", text)

    def test_empty_agent_credentials_preserved(self):
        self.agent.app_token = "fictional-token"
        db.session.commit()
        self.login()
        self.post(f"/agents/{self.agent.id}/edit", data={"app_token": ""})
        db.session.refresh(self.agent)
        self.assertEqual(self.agent.app_token, "fictional-token")


if __name__ == "__main__":
    unittest.main()
