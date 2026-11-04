"""Disposable, offline local acceptance server. Never uses existing databases."""
import os
from secrets import token_urlsafe
os.environ["LOGISTICS_ISOLATED"] = "1"

from app import app, db, User
from models import Customer, CarrierAgent, Shipment, ManualTrack, BankAccount


def seed():
    assert app.config.get("ISOLATED_MODE")
    with app.app_context():
        db.create_all()
        user = User(username="local-admin", is_admin=True)
        admin_password = token_urlsafe(18)
        user.set_password(admin_password)
        reader = User(username="local-reader", is_admin=False)
        reader_password = token_urlsafe(18)
        reader.set_password(reader_password)
        customer = Customer(name="虚构客户")
        agent = CarrierAgent(name="虚构手工代理", supports_api=False)
        shipment = Shipment(tracking_number="DEMO-001", shipment_id="DEMO-PROVIDER-001",
                            customer=customer, agent=agent, origin="虚构仓库",
                            destination="测试目的地", weight=2, unit_price=10, fee=20)
        db.session.add_all([user, reader, shipment])
        db.session.flush()
        db.session.add(ManualTrack(shipment_id=shipment.id, description="虚构包裹已入库"))
        second = Shipment(tracking_number='DEMO-002', customer=customer, agent=agent,
                          destination='测试目的地乙', weight=3, unit_price=12, pieces=2, fee=36)
        db.session.add(second)
        db.session.add(BankAccount(bank_name='虚构演示银行', account_name='虚构物流公司', account_no='DEMO-NOT-A-REAL-ACCOUNT'))
        db.session.flush()
        db.session.add(ManualTrack(shipment_id=second.id, description='虚构包裹等待出库', location='演示仓库'))
        db.session.commit()
        from isolated_runtime import register_shadow_fixture
        register_shadow_fixture(app, shipment)
        return admin_password, reader_password


if __name__ == "__main__":
    admin_password, reader_password = seed()
    print(f"Local demo: local-admin / {admin_password}, local-reader / {reader_password}", flush=True)
    app.run(host="127.0.0.1", port=int(os.environ.get("LOGISTICS_LOCAL_PORT", "5055")), debug=False, use_reloader=False)
