from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

# =========================
# 基础模型
# =========================
class TimestampMixin:
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# -------------------------
# 客户
# -------------------------
class Customer(db.Model, TimestampMixin):
    __tablename__ = "customer"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    email = db.Column(db.String(120), unique=True)
    phone = db.Column(db.String(50))

    # 可选：默认银行信息（纯文本）
    bank_info = db.Column(db.Text)

    shipments = db.relationship("Shipment", backref="customer", lazy=True)

# -------------------------
# 代理（货代）
# -------------------------
class CarrierAgent(db.Model, TimestampMixin):
    __tablename__ = "carrier_agent"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)

    # API 连接信息
    api_url = db.Column(db.String(1000))
    username = db.Column(db.String(200))
    password = db.Column(db.String(200))
    app_key = db.Column(db.String(255))
    app_token = db.Column(db.String(255))
    customer_code = db.Column(db.String(255))

    # 控制项
    supports_api = db.Column(db.Boolean, default=True)   # 不支持抓取时置 False
    is_active = db.Column(db.Boolean, default=True)      # 软删除

    shipments = db.relationship("Shipment", backref="agent", lazy=True)

    def __repr__(self):
        return f"<CarrierAgent {self.name}>"

# -------------------------
# 运单
# -------------------------
class Shipment(db.Model, TimestampMixin):
    __tablename__ = "shipment"

    id = db.Column(db.Integer, primary_key=True)
    tracking_number = db.Column(db.String(50), nullable=False)  # 客户单号
    shipment_id = db.Column(db.String(50))  # NextSLS 系统单号，替换 agent_tracking_number
    third_party_tracking_number = db.Column(db.String(50))  # 第三方快递单号（用于17track）

    # 关联
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    agent_id = db.Column(db.Integer, db.ForeignKey("carrier_agent.id"))
    carrier_id = db.Column(db.String(50))  # 环境货代 id（如 rtb56）

    # 基本信息
    origin = db.Column(db.String(100))
    destination = db.Column(db.String(100))
    channel = db.Column(db.String(200))     # 运输渠道（快递/空运/海运/铁路…）
    product_type = db.Column(db.String(200))
    pieces = db.Column(db.Integer, default=1)

    # 费用
    weight = db.Column(db.Float, default=0.0)
    unit_price = db.Column(db.Float, default=0.0)
    fee = db.Column(db.Float, default=0.0)              # 合计（自动计算或手填）
    surcharge_extra = db.Column(db.Float, default=0.0)  # 附加费（选填）
    operation_fee = db.Column(db.Float, default=0.0)    # 操作费（选填）
    high_value_fee = db.Column(db.Float, default=0.0)   # 超值费（选填）

    status = db.Column(db.String(50), default="已创建")
    note = db.Column(db.Text)

# -------------------------
# 手工轨迹
# -------------------------
class ManualTrack(db.Model, TimestampMixin):
    __tablename__ = "manual_track"

    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipment.id"), nullable=False, index=True)
    happen_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    location = db.Column(db.String(255))
    description = db.Column(db.Text, nullable=False)
    author = db.Column(db.String(100))  # 记录是谁写的（可选）

    # ORM cascade only: no database schema migration is needed.
    shipment = db.relationship(
        "Shipment", backref=db.backref("manual_tracks", cascade="all, delete-orphan")
    )

# -------------------------
# 收款账户（财务）
# -------------------------
class BankAccount(db.Model, TimestampMixin):
    __tablename__ = "bank_account"

    id = db.Column(db.Integer, primary_key=True)
    # 类型：private / public
    account_type = db.Column(db.String(20), default="private")  # private=私人, public=公账
    bank_name = db.Column(db.String(200), nullable=False)       # 开户行
    account_name = db.Column(db.String(200), nullable=False)    # 户名
    account_no = db.Column(db.String(200), nullable=False)      # 账号
    is_default = db.Column(db.Boolean, default=False)           # 导出时默认使用

    # 备注
    remark = db.Column(db.String(500))
