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


class TmsReceivable(db.Model, TimestampMixin):
    """One pricing draft or confirmed receivable per order, linked to one legacy shipment."""
    __tablename__ = "tms_receivable"
    __table_args__ = (
        db.CheckConstraint(
            "(shipment_id IS NULL AND confirmed_by IS NULL AND confirmed_at IS NULL "
            "AND total IS NULL AND currency IS NULL) OR "
            "(shipment_id IS NOT NULL AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL "
            "AND agent_id IS NOT NULL AND agent_service IS NOT NULL AND total IS NOT NULL AND total >= 0 "
            "AND currency IS NOT NULL)",
            name="ck_tms_receivable_confirmation"),
    )
    order_id = db.Column(db.Integer, db.ForeignKey("tms_order.id", ondelete="RESTRICT"), primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipment.id", ondelete="RESTRICT"), unique=True)
    agent_id = db.Column(db.Integer, db.ForeignKey("carrier_agent.id", ondelete="RESTRICT"))
    agent_service = db.Column(db.String(100))
    details = db.Column(db.JSON, nullable=False)
    total = db.Column(db.Numeric(18, 2))
    currency = db.Column(db.String(3))
    confirmed_by = db.Column(db.Integer)
    confirmed_at = db.Column(db.DateTime)


class TmsChargeAdjustment(db.Model, TimestampMixin):
    __tablename__ = "tms_charge_adjustment"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("tms_receivable.order_id", ondelete="RESTRICT"),
                         nullable=False, index=True)
    previous = db.Column(db.JSON, nullable=False)
    revised = db.Column(db.JSON, nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)


class TmsAgentApi(db.Model, TimestampMixin):
    __tablename__ = "tms_agent_api"
    agent_id = db.Column(db.Integer, db.ForeignKey("carrier_agent.id", ondelete="RESTRICT"), primary_key=True)
    provider = db.Column(db.String(16), nullable=False)
    base_url = db.Column(db.String(255), nullable=False)
    enabled = db.Column(db.JSON, nullable=False)
    service_codes = db.Column(db.JSON, nullable=False)
    sender = db.Column(db.JSON, nullable=False)
    verified = db.Column(db.JSON, nullable=False)


class TmsSupplierOrder(db.Model, TimestampMixin):
    __tablename__ = "tms_supplier_order"
    __table_args__ = (
        db.UniqueConstraint("agent_id", "supplier_number", name="uq_tms_supplier_number"),
        db.CheckConstraint("state IN ('new','sending','uncertain','not_found','linked')",
                           name="ck_tms_supplier_order_state"),
        db.CheckConstraint("(state = 'linked' AND supplier_number IS NOT NULL) OR "
                           "(state != 'linked' AND supplier_number IS NULL)",
                           name="ck_tms_supplier_number_link"),
    )
    order_id = db.Column(db.Integer, db.ForeignKey("tms_order.id", ondelete="RESTRICT"), primary_key=True)
    agent_id = db.Column(db.Integer, db.ForeignKey("carrier_agent.id", ondelete="RESTRICT"), nullable=False)
    client_reference = db.Column(db.String(50), nullable=False, unique=True)
    supplier_number = db.Column(db.String(50))
    tenant_origin = db.Column(db.String(255))
    state = db.Column(db.String(16), nullable=False, default="new")
    attempt_at = db.Column(db.DateTime)
    synced_at = db.Column(db.DateTime)


class TmsSupplierEvent(db.Model, TimestampMixin):
    __tablename__ = "tms_supplier_event"
    __table_args__ = (
        db.Index("ix_tms_supplier_event_order_kind", "order_id", "kind", "id"),
        db.CheckConstraint("kind IN ('snapshot','cost','attempt','reconcile','retry')", name="ck_tms_supplier_event_kind"),
    )
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("tms_supplier_order.order_id", ondelete="RESTRICT"), nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    details = db.Column(db.JSON, nullable=False)
