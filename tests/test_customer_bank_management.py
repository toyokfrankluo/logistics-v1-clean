"""Offline regression tests for customer, bank-account and invoice workflows."""
import io
import unittest

from openpyxl import load_workbook

import test_v1 as base
from models import db, Customer, BankAccount


class CustomerBankManagement(unittest.TestCase):
    setUpClass = classmethod(base.StabilityTests.setUpClass.__func__)
    setUp = base.StabilityTests.setUp
    tearDown = base.StabilityTests.tearDown
    token = base.StabilityTests.token
    post = base.StabilityTests.post
    login = base.StabilityTests.login

    def test_bank_account_create_and_edit_preserve_leading_zeroes(self):
        self.login()
        created = self.post("/bank_accounts", data={
            "account_type": "private", "bank_name": "虚构开户行", "account_name": "虚构户名",
            "account_no": "0000012300", "remark": "虚构备注",
        })
        self.assertEqual(created.status_code, 302)
        account = BankAccount.query.one()
        self.assertEqual(account.account_no, "0000012300")
        edited = self.post(f"/bank_accounts/{account.id}/edit", data={
            "account_type": "public", "bank_name": "虚构新开户行", "account_name": "虚构新户名",
            "account_no": "00000045", "remark": "更新备注", "is_default": "1",
        })
        self.assertEqual(edited.status_code, 302)
        db.session.refresh(account)
        self.assertEqual((account.account_type, account.bank_name, account.account_no),
                         ("public", "虚构新开户行", "00000045"))
        self.assertTrue(account.is_default)

    def test_bank_account_validation_and_csrf(self):
        self.login()
        response = self.post("/bank_accounts", data={
            "account_type": "unknown", "bank_name": "", "account_name": "x", "account_no": "1",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BankAccount.query.count(), 0)
        no_token = self.client.post("/bank_accounts", data={"bank_name": "虚构", "account_name": "虚构", "account_no": "001"})
        self.assertEqual(no_token.status_code, 400)
        self.assertEqual(BankAccount.query.count(), 0)

    def test_bank_account_delete_and_referenced_account_protection(self):
        self.login()
        account = BankAccount(bank_name="虚构银行", account_name="虚构户名", account_no="0000099")
        db.session.add(account)
        db.session.commit()
        # Simulate a legacy financial table with a real FK, without changing the app schema.
        db.session.execute(db.text(
            "CREATE TABLE legacy_invoice_test (id INTEGER PRIMARY KEY, bank_account_id INTEGER "
            "REFERENCES bank_account(id))"))
        db.session.execute(db.text("INSERT INTO legacy_invoice_test(bank_account_id) VALUES (:id)"), {"id": account.id})
        db.session.commit()
        try:
            response = self.post(f"/bank_accounts/{account.id}/delete")
            self.assertEqual(response.status_code, 302)
            self.assertIsNotNone(db.session.get(BankAccount, account.id))
        finally:
            db.session.execute(db.text("DROP TABLE legacy_invoice_test"))
            db.session.commit()
        self.assertEqual(self.post(f"/bank_accounts/{account.id}/delete").status_code, 302)
        self.assertIsNone(db.session.get(BankAccount, account.id))

    def test_default_account_is_single_and_export_uses_only_selected(self):
        self.login()
        account_a = BankAccount(bank_name="虚构银行甲", account_name="虚构户名甲", account_no="00001111")
        account_b = BankAccount(bank_name="虚构银行乙", account_name="虚构户名乙", account_no="00002222")
        db.session.add_all([account_a, account_b])
        db.session.commit()
        self.assertEqual(self.post("/finance", data={"export": "1"}).status_code, 400)
        self.assertEqual(self.post(f"/bank_accounts/{account_a.id}/default").status_code, 302)
        self.assertEqual(self.post(f"/bank_accounts/{account_b.id}/default").status_code, 302)
        db.session.refresh(account_a); db.session.refresh(account_b)
        self.assertFalse(account_a.is_default)
        self.assertTrue(account_b.is_default)
        page = self.client.get("/finance").get_data(as_text=True)
        self.assertIn("虚构银行乙", page)
        self.assertIn("****2222", page)
        self.assertNotIn("00002222", page)
        response = self.post("/finance", data={"export": "1", "bank_account_id": str(account_a.id)})
        self.assertEqual(response.status_code, 200)
        values = [cell.value for row in load_workbook(io.BytesIO(response.data)).active.iter_rows() for cell in row]
        self.assertIn("账号：00001111", values)
        self.assertNotIn("账号：00002222", values)

    def test_export_rejects_invalid_account_ids(self):
        self.login()
        for account_id in ("not-a-number", "999999"):
            response = self.post("/finance", data={"export": "1", "bank_account_id": account_id})
            self.assertEqual(response.status_code, 400)

    def test_bank_and_customer_mutations_require_admin_and_csrf(self):
        self.login(admin=False)
        self.assertEqual(self.client.get(f"/bank_accounts/{1}/edit").status_code, 403)
        self.assertEqual(self.client.get(f"/customers/{self.customer.id}/edit").status_code, 403)
        for path in ("/bank_accounts/1/delete", f"/customers/{self.customer.id}/delete"):
            self.assertEqual(self.client.post(path).status_code, 403)
        self.login()
        account = BankAccount(bank_name="虚构", account_name="虚构", account_no="0001")
        db.session.add(account); db.session.commit()
        self.assertEqual(self.client.post(f"/bank_accounts/{account.id}/delete").status_code, 400)

    def test_customer_edit_duplicate_name_and_association_preserved(self):
        self.login()
        other = Customer(name="虚构客户乙")
        db.session.add(other); db.session.commit()
        response = self.post(f"/customers/{self.customer.id}/edit", data={
            "name": "虚构客户乙", "bankinfo": "虚构银行资料",
        })
        self.assertEqual(response.status_code, 400)
        response = self.post(f"/customers/{self.customer.id}/edit", data={
            "name": "虚构客户甲更新", "bankinfo": "虚构银行资料", "email": "fake@example.invalid", "phone": "000-FAKE",
        })
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.customer); db.session.refresh(self.shipment)
        self.assertEqual(self.customer.name, "虚构客户甲更新")
        self.assertEqual(self.shipment.customer_id, self.customer.id)
        self.assertEqual(self.customer.email, "fake@example.invalid")
        self.assertEqual(self.customer.phone, "000-FAKE")
        self.assertEqual(self.post(f"/customers/{self.customer.id}/edit", data={"name": " "}).status_code, 400)
        self.assertEqual(self.client.post(f"/customers/{other.id}/edit", data={"name": "重复"}).status_code, 400)

    def test_referenced_customer_cannot_delete_but_unreferenced_can(self):
        self.login()
        response = self.post(f"/customers/{self.customer.id}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(db.session.get(Customer, self.customer.id))
        other = Customer(name="无关联虚构客户")
        db.session.add(other); db.session.commit()
        response = self.post(f"/customers/{other.id}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(Customer, other.id))

    def test_customer_delete_csrf_and_customer_edit_login(self):
        self.assertEqual(self.client.get(f"/customers/{self.customer.id}/edit").status_code, 302)
        self.login()
        self.assertEqual(self.client.post(f"/customers/{self.customer.id}/delete").status_code, 400)


if __name__ == "__main__":
    unittest.main()
