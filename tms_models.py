"""Additive tables for customer ordering. Shipment remains the legacy transport record."""
from models import db, TimestampMixin


class TmsAccount(db.Model):
    __tablename__ = "tms_account"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id", ondelete="RESTRICT"),
                            nullable=False, index=True)
    username = db.Column(db.String(80), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)


class TmsOrder(db.Model, TimestampMixin):
    __tablename__ = "tms_order"
    __table_args__ = (
        db.UniqueConstraint("customer_id", "number_key", name="uq_tms_order_customer_number"),
        db.UniqueConstraint("customer_id", "file_sha256", name="uq_tms_order_customer_file"),
        db.CheckConstraint("status IN ('draft','pending','approved','rejected')",
                           name="ck_tms_order_status"),
        db.Index("ix_tms_order_customer_status", "customer_id", "status", "id"),
        db.Index("ix_tms_order_review_status", "status", "id"),
    )
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id", ondelete="RESTRICT"),
                            nullable=False)
    number_key = db.Column(db.String(50), nullable=False)
    file_sha256 = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="draft")
    invoice = db.Column(db.JSON, nullable=False)
    workbook = db.deferred(db.Column(db.LargeBinary, nullable=False))
    agent_id = db.Column(db.Integer, db.ForeignKey("carrier_agent.id", ondelete="RESTRICT"))
    agent_service = db.Column(db.String(100))
    rejection_reason = db.Column(db.String(500))
    revision = db.Column(db.Integer, nullable=False, default=1)


class TmsAudit(db.Model, TimestampMixin):
    __tablename__ = "tms_audit"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("tms_order.id", ondelete="RESTRICT"),
                         nullable=False, index=True)
    actor_type = db.Column(db.String(12), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(20), nullable=False)
