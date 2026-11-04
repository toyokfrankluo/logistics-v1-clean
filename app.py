# app.py — 完整后端（含 5 大需求）
import os
import json
import time
import sqlite3
import traceback
from datetime import datetime
from pathlib import Path
from contextlib import closing
from flask_migrate import Migrate

import requests
from utils import generate_invoice_xlsx
from werkzeug.security import generate_password_hash, check_password_hash

from flask import (
    Flask, request, render_template, render_template_string,
    redirect, url_for, flash, send_file, abort
)
from flask_cors import CORS


try:
    import pandas as pd
except Exception:
    pd = None

from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required, current_user
)
from jinja2 import TemplateNotFound

# 使用你提供的 models.py 定义
from models import db, Customer, CarrierAgent, Shipment, ManualTrack, BankAccount

# ------------------------------
# 基本应用与配置
# ------------------------------
ISOLATED_MODE = os.getenv("LOGISTICS_ISOLATED") == "1"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
import secrets
app.secret_key = os.getenv("FLASK_SECRET") or secrets.token_hex(32)
if ISOLATED_MODE:
    from isolated_runtime import configure
    configure(app)

# Public queries are same-origin only. CORS is not authorization.
from public_tracking import init_app as init_public_tracking
import public_tracking
init_public_tracking(app)

from views import views
app.register_blueprint(views)

app.config["JSON_AS_ASCII"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True
if not ISOLATED_MODE:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url or not os.getenv("FLASK_SECRET", "").strip():
        raise RuntimeError("DATABASE_URL and FLASK_SECRET are required")
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url

    # External integrations require a separate, reviewed release.
    from isolated_runtime import deny_network
    requests.sessions.Session.request = deny_network
    requests.sessions.Session.send = deny_network
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
migrate = Migrate(app, db)

# 登录
login_manager = LoginManager(app)
login_manager.login_view = "login"
from write_security import csrf, init_write_security
init_write_security(app, login_manager)

# 管理后台
from access_control import SecureAdminIndexView, SecureModelView, OwnedRecordView
admin = Admin(app, name="后台管理", template_mode="bootstrap4",
              index_view=SecureAdminIndexView())

# 环境配置
DEFAULT_API_URL = os.getenv("API_URL", "http://ywsl.rtb56.com/webservice/PublicService.asmx/ServiceInterfaceUTF8")
CARRIERS_LIST = [c.strip() for c in os.getenv("CARRIERS", "rtb56").split(",") if c.strip()]
CARRIERS = {}
for cid in CARRIERS_LIST:
    key_prefix = cid.upper()
    CARRIERS[cid] = {
        "id": cid,
        "name": os.getenv(f"{key_prefix}_NAME", cid),
        "token": os.getenv(f"{key_prefix}_TOKEN"),
        "key": os.getenv(f"{key_prefix}_KEY"),
        "api_url": os.getenv(f"{key_prefix}_API_URL", DEFAULT_API_URL)
    }

PUBLIC_MODE = os.getenv("PUBLIC_MODE", "0") == "1"  # 公共查询页屏蔽批量

# 简单缓存
CACHE = {}
CACHE_TTL = int(os.getenv("CACHE_TTL", "600"))

# 17Track API配置
TRACK17_API_KEY = os.getenv("TRACK17_API_KEY", "")
TRACK17_API_URL = os.getenv("TRACK17_API_URL", "https://api.17track.net/track/v2/gettrackinfo")

# ------------------------------
# 工具：sqlite 文件路径
# ------------------------------
def get_sqlite_path_from_uri(uri: str):
    if not uri:
        return None
    uri = uri.strip()
    if uri.startswith("sqlite:///"):
        return uri.replace("sqlite:///", "", 1)
    if uri.startswith("sqlite:////"):
        return "/" + uri.replace("sqlite:////", "", 1)
    return None

# ------------------------------
# 用户模型（仅后端使用）
# ------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

# Admin
admin.add_view(SecureModelView(User, db.session))
admin.add_view(SecureModelView(Customer, db.session))
admin.add_view(SecureModelView(CarrierAgent, db.session))
admin.add_view(OwnedRecordView(Shipment, db.session))
admin.add_view(OwnedRecordView(ManualTrack, db.session))
admin.add_view(SecureModelView(BankAccount, db.session))

# ------------------------------
# 登录回调
# ------------------------------
@login_manager.user_loader
def load_user(user_id):
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None

# ------------------------------
# 模板安全渲染
# ------------------------------
def render_template_safe(template_name, **context):
    try:
        return render_template(template_name, **context)
    except TemplateNotFound:
        fallback = f"""
        <!doctype html>
        <html>
        <head><meta charset="utf-8"><title>{template_name} - 占位</title></head>
        <body style="font-family: -apple-system, Arial;">
        <h2>缺少页面模板：{template_name}</h2>
        <p>请在 <code>templates/</code> 目录中添加 <strong>{template_name}</strong> 模板文件。</p>
        <pre>渲染数据（调试用）:</pre>
        <div style="white-space:pre-wrap;border:1px solid #ddd;padding:10px;">{context}</div>
        <p><a href="{url_for('index')}">返回首页</a></p>
        </body>
        </html>
        """
        return render_template_string(fallback)

# ------------------------------
# 启动时自动补列（SQLite）
# ------------------------------
def ensure_sqlite_columns():
    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    sqlite_path = get_sqlite_path_from_uri(uri)
    if not sqlite_path:
        app.logger.info("非 SQLite，跳过自动补列。")
        return

    db_file = Path(sqlite_path)
    if not db_file.exists():
        app.logger.info(f"SQLite {sqlite_path} 不存在，create_all 已创建。")
        return

    app.logger.info(f"开始检查并补列 SQLite 数据库: {sqlite_path}")
    try:
        with closing(sqlite3.connect(str(db_file))) as conn:
            cur = conn.cursor()

            def table_has_col(table, col):
                cur.execute(f"PRAGMA table_info('{table}')")
                rows = cur.fetchall()
                cols = [r[1] for r in rows]
                return col in cols

            patches = {
                "carrier_agent": [
                    ("api_url", "TEXT"), ("username", "TEXT"), ("password", "TEXT"),
                    ("app_key", "TEXT"), ("app_token", "TEXT"), ("customer_code", "TEXT"),
                    ("supports_api", "INTEGER DEFAULT 1"), ("is_active", "INTEGER DEFAULT 1")
                ],
                "shipment": [
                    ("customer_id", "INTEGER"), ("agent_id", "INTEGER"), ("carrier_id", "TEXT"),
                    ("origin", "TEXT"), ("destination", "TEXT"), ("channel", "TEXT"),
                    ("product_type", "TEXT"), ("pieces", "INTEGER DEFAULT 1"),
                    ("weight", "REAL DEFAULT 0"), ("unit_price", "REAL DEFAULT 0"),
                    ("fee", "REAL DEFAULT 0"), ("surcharge_extra", "REAL DEFAULT 0"),
                    ("operation_fee", "REAL DEFAULT 0"), ("high_value_fee", "REAL DEFAULT 0"),
                    ("status", "TEXT"), ("note", "TEXT"),
                    ("agent_tracking_number", "TEXT"), ("third_party_tracking_number", "TEXT"),
                    ("created_at", "TEXT"), ("updated_at", "TEXT")
                ],
                "manual_track": [
                    ("shipment_id", "INTEGER"), ("happen_time", "TEXT"),
                    ("location", "TEXT"), ("description", "TEXT"), ("author", "TEXT"),
                    ("created_at", "TEXT"), ("updated_at", "TEXT")
                ],
                "bank_account": [
                    ("account_type", "TEXT"), ("bank_name", "TEXT"),
                    ("account_name", "TEXT"), ("account_no", "TEXT"),
                    ("is_default", "INTEGER DEFAULT 0"), ("remark", "TEXT"),
                    ("created_at", "TEXT"), ("updated_at", "TEXT")
                ],
                "customer": [
                    ("bank_info", "TEXT"),
                    ("created_at", "TEXT"), ("updated_at", "TEXT")
                ]
            }

            for table, cols in patches.items():
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
                if cur.fetchone() is None:
                    app.logger.info(f"表 {table} 不存在，跳过补列（create_all 已处理）。")
                    continue
                app.logger.info(f"检查表 {table} 的列...")
                for col, typ in cols:
                    if not table_has_col(table, col):
                        sql = f"ALTER TABLE {table} ADD COLUMN {col} {typ}"
                        try:
                            cur.execute(sql)
                            app.logger.info(f"成功为 {table} 添加列 {col} ({typ})")
                        except Exception as e:
                            app.logger.error(f"为 {table} 添加列 {col} 失败: {str(e)}")
                    else:
                        app.logger.debug(f"表 {table} 已存在列 {col}，跳过。")

            conn.commit()
            app.logger.info("补列操作完成。")
    except Exception as e:
        app.logger.exception(f"补列过程中发生全局错误: {str(e)}")

# ------------------------------
# 17Track API 调用函数
# ------------------------------
def call_17track(tracking_number):
    """调用17Track API获取轨迹信息"""
    if not TRACK17_API_KEY:
        return {"error": "17Track API密钥未配置"}
    
    try:
        headers = {
            "Content-Type": "application/json",
            "17token": TRACK17_API_KEY
        }
        
        payload = {
            "number": tracking_number,
            "carrier": None  # 自动识别快递公司
        }
        
        response = requests.post(TRACK17_API_URL, headers=headers, json=payload, timeout=15)
        
        if response.status_code != 200:
            return {"error": f"17Track API返回错误 {response.status_code}"}
            
        data = response.json()
        
        # 检查API响应状态
        if data.get("status") != 200:
            return {"error": f"17Track API错误: {data.get('message', '未知错误')}"}
            
        # 解析轨迹数据
        tracks = []
        if data.get("data") and isinstance(data["data"], list) and len(data["data"]) > 0:
            for event in data["data"][0].get("track", []):
                tracks.append({
                    "track_occur_date": event.get("time", ""),
                    "track_location": event.get("location", ""),
                    "track_description": event.get("description", event.get("info", ""))
                })
        
        return {
            "success": "1",
            "cnmessage": "17Track轨迹查询成功",
            "data": [{"details": tracks}]
        }
        
    except Exception as e:
        return {"error": f"17Track API请求失败: {str(e)}"}

# ------------------------------
# NextSLS API 调用函数（通用版本）
# ------------------------------
def call_nextsls(agent, tracking_number):
    """调用NextSLS API获取轨迹信息（通用版本）"""
    try:
        # 使用代理配置的API URL
        url = agent.api_url
        if not url:
            return {"error": "未配置NextSLS API地址"}
        
        # 根据NextSLS文档构建请求头
        headers = {
            "Content-Type": "application/json"
        }
        
        # 添加认证信息（根据不同代理的配置方式）
        if agent.app_token:
            headers["Authorization"] = f"Bearer {agent.app_token}"
        
        # 构建请求体 - 支持多种查询方式
        payload = {}
        
        # 方式1: 使用客户参考号（client_reference）查询
        payload["shipment"] = {
            "client_reference": tracking_number,
            "language": "zh"
        }
        
        # 方式2: 如果有access_token参数，添加到请求中（某些NextSLS版本需要）
        if agent.app_token and "access_token" not in payload:
            payload["access_token"] = agent.app_token
            
        # 方式3: 某些NextSLS版本可能需要不同的参数结构
        # 这里可以根据代理的具体配置进行调整
        
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        
        if response.status_code != 200:
            return {"error": f"NextSLS API返回错误 {response.status_code}: {response.text}"}
            
        data = response.json()
        
        # 检查API响应状态 - 支持多种响应格式
        if data.get("status") not in [1, "1", 200, "200", True]:
            error_msg = data.get("info") or data.get("message") or data.get("error") or "未知错误"
            return {"error": f"NextSLS API错误: {error_msg}"}
            
        # 解析轨迹数据 - 支持多种响应格式
        tracks = []
        
        # 格式1: data.shipment.traces (标准NextSLS格式)
        if data.get("data") and data["data"].get("shipment"):
            shipment_data = data["data"]["shipment"]
            for trace in shipment_data.get("traces", []):
                # 转换时间戳为日期时间字符串
                timestamp = trace.get("time")
                if timestamp and isinstance(timestamp, int):
                    time_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    time_str = trace.get("time", "")
                    
                tracks.append({
                    "track_occur_date": time_str,
                    "track_location": trace.get("location", ""),
                    "track_description": trace.get("info", trace.get("description", ""))
                })
        
        # 格式2: 直接包含tracks数组
        elif data.get("tracks"):
            for trace in data.get("tracks", []):
                tracks.append({
                    "track_occur_date": trace.get("occur_date", trace.get("time", "")),
                    "track_location": trace.get("location", ""),
                    "track_description": trace.get("info", trace.get("description", ""))
                })
        
        # 格式3: 其他可能的格式
        elif data.get("data") and isinstance(data["data"], list):
            for item in data["data"]:
                tracks.append({
                    "track_occur_date": item.get("occur_date", item.get("time", "")),
                    "track_location": item.get("location", ""),
                    "track_description": item.get("info", item.get("description", ""))
                })
        
        # 如果没有找到轨迹数据，返回错误
        if not tracks:
            return {"error": "未找到轨迹信息"}
        
        return {
            "success": "1",
            "cnmessage": "NextSLS轨迹查询成功",
            "data": [{"details": tracks}]
        }
        
    except Exception as e:
        return {"error": f"NextSLS API请求失败: {str(e)}"}

# ------------------------------
# 多货代 API：gettrack（TXFBA 强制 POST 版本）
# ------------------------------
def call_gettrack(carrier_id=None, tracking_number=None, agent_id=None, timeout=15):
    if not tracking_number:
        return {"error": "必须提供 tracking_number"}

    shipment = Shipment.query.filter_by(tracking_number=tracking_number).first()
    if not shipment:
        return {"error": f"未找到运单号: {tracking_number}"}

    cache_key = (f"agent:{agent_id}" if agent_id else f"carrier:{carrier_id}", tracking_number)
    now = time.time()
    if cache_key in CACHE:
        ts, data = CACHE[cache_key]
        if now - ts < CACHE_TTL:
            return data

    # ------------------------------
    # 本地手工轨迹
    # ------------------------------
    def local_manual():
        s = Shipment.query.filter_by(tracking_number=tracking_number).first()
        if not s or not s.manual_tracks:
            return None
        details = []
        for t in sorted(s.manual_tracks, key=lambda x: (x.happen_time or x.created_at or datetime.utcnow()), reverse=True):
            details.append({
                "track_occur_date": (t.happen_time or t.created_at).strftime("%Y-%m-%d %H:%M:%S"),
                "track_location": t.location or "",
                "track_description": t.description
            })
        return {
            "success": "1",
            "cnmessage": "手工轨迹",
            "data": [{"details": details}]
        }

    # ------------------------------
    # 获取 shipment
    # ------------------------------
    shipment = Shipment.query.filter_by(tracking_number=tracking_number).first()
    if not shipment:
        return {"error": f"未找到运单号: {tracking_number}"}

    shipment_id_to_use = shipment.shipment_id

    # ------------------------------
    # 使用 DB agent
    # ------------------------------
    if agent_id or shipment.agent_id:
        agent = CarrierAgent.query.get(int(agent_id or shipment.agent_id))
        if not agent or not agent.is_active:
            data = {"error": "未找到指定代理或已停用"}
            CACHE[cache_key] = (now, data)
            return data

        if not agent.supports_api:
            data = local_manual() or {"error": "该代理不支持抓取，且无手工轨迹"}
            CACHE[cache_key] = (now, data)
            return data

        # ----------------
        # NextSLS 逻辑
        # ----------------
        is_nextsls = "nextsls" in (agent.api_url or "").lower() or "sls" in (agent.api_url or "").lower()
        if is_nextsls:
            if not shipment_id_to_use:
                return {"error": "未配置代理系统单号"}
            payload = {
                "shipment": {
                    "shipment_id": shipment_id_to_use,
                    "language": "zh"
                }
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {agent.app_token or ''}"
            }
            try:
                r = requests.post(agent.api_url, json=payload, headers=headers, timeout=timeout)
                r.encoding = "utf-8"
                data = r.json()
                if data.get("status") != 1:
                    data["hint"] = f"疑似 shipment_id 错误: {shipment_id_to_use}"
                CACHE[cache_key] = (now, data)
                return data
            except Exception as e:
                data = {"error": f"请求代理接口出错: {e}"}
                CACHE[cache_key] = (now, data)
                return data

        # ----------------
        # TXFBA 强制 POST
        # ----------------
        if "txfba.com" in (agent.api_url or "").lower():
            payload = {
                "appToken": agent.app_token or "",
                "appKey": agent.app_key or "",
                "serviceMethod": "gettrack",
                "paramsJson": json.dumps({"tracking_number": tracking_number}, ensure_ascii=False)
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded"}

            # 🔹 调试打印
            print("TXFBA 请求 URL:", agent.api_url)
            print("物流操作完成或失败；详细数据不写入日志")
            print("物流操作完成或失败；详细数据不写入日志")

            try:
                r = requests.post(agent.api_url, data=payload, headers=headers, timeout=timeout)
                r.encoding = "utf-8"
                try:
                    data = r.json()
                except Exception:
                    data = {"raw_text": r.text}
                if isinstance(data, dict) and data.get("success") == "0" and "appToken" in (data.get("cnmessage") or ""):
                    data["hint"] = "疑似 appKey/appToken 或客户号配置错误，或该代理未开通 API 权限"
                CACHE[cache_key] = (now, data)
                return data
            except Exception as e:
                data = {"error": f"请求代理接口出错: {e}"}
                CACHE[cache_key] = (now, data)
                return data

        # ----------------
        # RTB56 / 其他代理逻辑
        # ----------------
        if agent.app_key or agent.app_token:
            try:
                payload = {
                    "appToken": agent.app_token or "",
                    "appKey": agent.app_key or "",
                    "serviceMethod": "gettrack",
                    "paramsJson": json.dumps({"tracking_number": tracking_number}, ensure_ascii=False)
                }
                headers = {"Content-Type": "application/x-www-form-urlencoded"}
                r = requests.post(agent.api_url, data=payload, headers=headers, timeout=timeout)
                r.encoding = "utf-8"
                try:
                    data = r.json()
                except Exception:
                    data = {"raw_text": r.text}
                CACHE[cache_key] = (now, data)
                return data
            except Exception as e:
                data = {"error": f"请求代理接口出错: {e}"}
                CACHE[cache_key] = (now, data)
                return data

        # ----------------
        # 用户名/密码方式
        # ----------------
        try:
            payload = {
                "username": agent.username or "",
                "password": agent.password or "",
                "tracking_number": tracking_number
            }
            r = requests.post(agent.api_url, data=payload, timeout=timeout)
            r.encoding = "utf-8"
            try:
                data = r.json()
            except Exception:
                data = {"raw_text": r.text}
            CACHE[cache_key] = (now, data)
            return data
        except Exception as e:
            data = {"error": f"请求代理接口出错: {e}"}
            CACHE[cache_key] = (now, data)
            return data

    manual = local_manual()
    if manual:
        return manual

    # ----------------
    # 使用环境 carrier
    # ----------------
    carrier = CARRIERS.get(carrier_id) if carrier_id else CARRIERS.get(CARRIERS_LIST[0]) if CARRIERS_LIST else None
    if not carrier:
        data = {"error": "未配置可用的货代（env）"}
        CACHE[cache_key] = (now, data)
        return data

    payload = {
        "appToken": carrier.get("token"),
        "appKey": carrier.get("key"),
        "serviceMethod": "gettrack",
        "paramsJson": json.dumps({"tracking_number": tracking_number}, ensure_ascii=False)
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        r = requests.post(carrier.get("api_url", DEFAULT_API_URL), data=payload, headers=headers, timeout=timeout)
        r.encoding = "utf-8"
        try:
            data = r.json()
        except Exception:
            data = {"raw_text": r.text}
        if isinstance(data, dict) and data.get("success") == "0":
            local = local_manual()
            if local:
                data = local
        CACHE[cache_key] = (now, data)
        return data
    except Exception as e:
        data = local_manual() or {"error": f"请求出错: {e}"}
        CACHE[cache_key] = (now, data)
        return data

# ------------------------------
# 轨迹格式化
# ------------------------------
def format_tracks_from_data(data):
    if not data:
        return "没有返回数据"
    if isinstance(data, dict) and data.get("error"):
        return f"错误: {data.get('error')}"
    if isinstance(data, dict) and "raw_text" in data:
        return data["raw_text"][:10000]
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list) and data["data"]:
        first = data["data"][0]
        details = first.get("details") or []
        if not details:
            return data.get("cnmessage", "暂无轨迹信息")
        parts = []
        for d in details:
            t = (d.get("track_occur_date") or "").strip()
            loc = (d.get("track_location") or "").strip()
            desc = (d.get("track_description") or d.get("track_description_en") or "").strip()
            line = " — ".join([x for x in [loc, desc] if x])
            parts.append(f"{line}\n{t}".strip())
        return "\n\n".join(parts) if parts else "暂无轨迹信息"
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception:
        return str(data)

# ------------------------------
# 登录/登出
# ------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if not username or not password:
            flash("请输入用户名与密码")
            return redirect(url_for("login"))
        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash("用户名或密码错误")
            return redirect(url_for("login"))
        login_user(user)
        flash("登录成功")
        return redirect(url_for("index"))
    try:
        return render_template("login.html")
    except TemplateNotFound:
        return render_template_string("""
        <!doctype html><meta charset="utf-8"><title>登录</title>
        <h2>登录</h2>
        <form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            用户名: <input name="username"><br><br>
            密码: <input name="password" type="password"><br><br>
            <button type="submit">登录</button>
        </form>
        """)

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    """兼容旧端点名，业务处理统一到 views。"""
    return app.view_functions["views.logout"]()

# ------------------------------
# 首页
# ------------------------------
@app.route("/")
@login_required
def index():
    return redirect(url_for("shipments"))

# ------------------------------
# 代理管理：新增 / 列表（兼容你的模板）
# 新增：编辑、软删除
# ------------------------------
@app.route("/agents", methods=["GET", "POST"])
@login_required
def agents():
    """兼容旧端点名，业务处理统一到 views。"""
    return app.view_functions["views.agents"]()

@app.route("/agents/<int:agent_id>/edit", methods=["GET", "POST"])
@login_required
def edit_agent(agent_id):
    """兼容旧端点名，业务处理统一到 views。"""
    return app.view_functions["views.edit_agent"](agent_id)

@app.route("/agents/<int:agent_id>/delete", methods=["POST"])
@login_required
def delete_agent(agent_id):
    """兼容旧端点名，业务处理统一到 views。"""
    return app.view_functions["views.delete_agent"](agent_id)

# ------------------------------
# 运单管理：列表 / 新增 / 导入
# ------------------------------
@app.route("/shipments")
@login_required
def shipments():
    """兼容旧端点名，业务处理统一到 views。"""
    return app.view_functions["views.shipments"]()

def _calc_fee(weight, unit_price, surcharge_extra, operation_fee, high_value_fee, manual_fee):
    try:
        w = float(weight or 0)
        p = float(unit_price or 0)
        s1 = float(surcharge_extra or 0)
        s2 = float(operation_fee or 0)
        s3 = float(high_value_fee or 0)
        if manual_fee is not None and str(manual_fee).strip() != "":
            return float(manual_fee)
        return round(w * p + s1 + s2 + s3, 2)
    except Exception:
        return float(manual_fee or 0)

@app.route("/shipments/add", methods=["GET", "POST"])
@login_required
def add_shipment():
    """兼容旧端点名，业务处理统一到 views。"""
    return app.view_functions["views.add_shipment"]()

@app.route("/shipments/import", methods=["POST"])
@login_required
def import_shipments():
    if pd is None:
        flash("未安装 pandas，无法使用 Excel 导入。请安装 pandas")
        return redirect(url_for("shipments"))
    f = request.files.get("file")
    if not f:
        flash("未上传文件")
        return redirect(url_for("shipments"))
    try:
        df = pd.read_excel(f)
    except Exception as e:
        flash(f"读取 Excel 出错: {e}")
        return redirect(url_for("shipments"))
    count = 0
    for _, row in df.iterrows():
        tn = str(row.get("tracking_number", "")).strip()
        if not tn or Shipment.query.filter_by(tracking_number=tn).first():
            continue
        s = Shipment(
            tracking_number=tn,
            origin=row.get("origin", ""),
            destination=row.get("destination", ""),
            weight=float(row.get("weight") or 0),
            unit_price=float(row.get("unit_price") or 0),
            surcharge_extra=float(row.get("surcharge_extra") or 0),
            operation_fee=float(row.get("operation_fee") or 0),
            high_value_fee=float(row.get("high_value_fee") or 0),
            fee=_calc_fee(row.get("weight"), row.get("unit_price"),
                          row.get("surcharge_extra"), row.get("operation_fee"),
                          row.get("high_value_fee"), row.get("fee")),
            status="已导入"
        )
        db.session.add(s)
        count += 1
    db.session.commit()
    flash(f"共导入 {count} 条运单")
    return redirect(url_for("shipments"))

# 额外：运单编辑/删除（避免误录无法改）
@app.route("/shipments/<int:sid>/edit", methods=["GET", "POST"])
@login_required
def edit_shipment(sid):
    s = Shipment.query.get_or_404(sid)
    if request.method == "POST":
        from shipment_updates import update_shipment
        try:
            update_shipment(s, request.form, allow_manual_fee=True)
        except (ValueError, TypeError):
            db.session.rollback()
            flash("运单号重复、为空或数字格式无效")
            return redirect(url_for("edit_shipment", sid=sid))
        db.session.commit()
        flash("运单已更新")
        return redirect(url_for("shipments"))
    return render_template_string("""
    <h3>编辑运单 {{s.tracking_number}}</h3>
    <form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      代理系统单号 <input name="shipment_id" value="{{s.shipment_id or ''}}"><br>
      17Track单号 <input name="third_party_tracking_number" value="{{s.third_party_tracking_number or ''}}"><br>
      目的地 <input name="destination" value="{{s.destination or ''}}"><br>
      渠道 <input name="channel" value="{{s.channel or ''}}"><br>
      产品类型 <input name="product_type" value="{{s.product_type or ''}}"><br>
      件数 <input name="pieces" value="{{s.pieces or 1}}"><br>
      重量 <input name="weight" value="{{s.weight or 0}}"><br>
      单价 <input name="unit_price" value="{{s.unit_price or 0}}"><br>
      附加费 <input name="surcharge_extra" value="{{s.surcharge_extra or 0}}"><br>
      操作费 <input name="operation_fee" value="{{s.operation_fee or 0}}"><br>
      超值费 <input name="high_value_fee" value="{{s.high_value_fee or 0}}"><br>
      合计(可手填覆盖) <input name="fee" value="{{s.fee or 0}}"><br>
      备注 <input name="note" value="{{s.note or ''}}"><br><br>
      <button>保存</button> <a href="{{url_for('shipments')}}">返回</a>
    </form>
    """, s=s)

@app.route("/shipments/<int:sid>/delete", methods=["POST"])
@login_required
def delete_shipment(sid):
    from deletion import delete_records
    from sqlalchemy.exc import SQLAlchemyError
    s = Shipment.query.get_or_404(sid)
    try:
        delete_records([s])
    except SQLAlchemyError:
        return "删除失败，全部修改已回滚", 409
    flash("运单及手工轨迹已删除；客户、代理和收款账户保留", "success")
    return redirect(url_for("shipments"))

# ------------------------------
# 客户管理（兼容原有模板）
# ------------------------------
@app.route("/customers", methods=["GET", "POST"])
@login_required
def customers():
    """兼容旧端点名，业务处理统一到 views。"""
    return app.view_functions["views.customers"]()

# ------------------------------
# 财务模块（时间筛选 + 选用收款账户 + 导出）
# ------------------------------
@app.route("/finance", methods=["GET", "POST"])
@login_required
def finance():
    """兼容旧端点名，业务处理统一到 views。"""
    return app.view_functions["views.finance"]()

# 简易银行账户管理（占位路由，可在 Admin 里直接维护）
@app.route("/bank_accounts", methods=["GET", "POST"])
@login_required
def bank_accounts():
    """兼容旧端点名，业务处理统一到 views。"""
    return app.view_functions["views.bank_accounts"]()

# ------------------------------
# 手工轨迹：为不支持抓取的代理/单票添加
# ------------------------------
from datetime import datetime
from flask import request, redirect, url_for, flash, render_template_string, jsonify
from flask_login import login_required, current_user

# Keep the real tracking implementation above; do not import utility placeholders.

# ------------------------------
# 手工轨迹
# ------------------------------
@app.route("/shipments/<int:sid>/tracks", methods=["GET", "POST"])
@login_required
def manual_tracks(sid):
    """兼容旧端点名，业务处理统一到 views。"""
    return app.view_functions["views.manual_tracks"](sid)

# ------------------------------
# 内部轨迹查询（支持代理/客户）
# ------------------------------
@app.route("/track", methods=["GET", "POST"])
@login_required
def track():
    """兼容旧端点名，业务处理统一到 views。"""
    return app.view_functions["views.track"]()

# ------------------------------
# 公共查询页面（Web表单）
# ------------------------------
@app.route("/public/track", methods=["GET", "POST"])
def public_track_page():
    return public_tracking.page()

# ------------------------------
# API
# ------------------------------
@app.route("/api/track/<carrier_id>/<tracking_number>")
def api_track_one(carrier_id, tracking_number):
    return public_tracking.api_lookup(tracking_number, carrier_id=carrier_id)

@app.route("/api/track_by_agent/<int:agent_id>/<tracking_number>")
def api_track_by_agent(agent_id, tracking_number):
    return public_tracking.api_lookup(tracking_number, agent_id=agent_id)

# ------------------------------
# 17Track API 路由
# ------------------------------
@app.route("/api/17track/<tracking_number>")
def api_17track(tracking_number):
    return public_tracking.api_lookup(tracking_number, third=True)

# ------------------------------
# 新增路由，渲染前端页面
# ------------------------------
@app.route("/frontend")
def frontend_page():
    return render_template("public_frontend.html")

# ------------------------------
# 新增 JSON API（前端 fetch 调用用这个）
# ------------------------------
@app.route("/public_track", methods=["POST"])
@csrf.exempt
def public_track_json():
    return public_tracking.json_lookup()

# ------------------------------
# 账单手工导出（保留）
# ------------------------------
@app.route("/invoice", methods=["GET", "POST"])
@login_required
def invoice():
    if request.method == "GET":
        return render_template_safe("invoice.html")
    customer = request.form.get("customer", "").strip()
    bankinfo = request.form.get("bankinfo", "").strip()
    raw = request.form.get("rows", "").strip()
    if not raw:
        flash("请粘贴账单行")
        return redirect(url_for("invoice"))
    lines = [l for l in raw.splitlines() if l.strip()]
    parsed = []
    for ln in lines:
        cols = [c.strip() for c in (ln.split("\t") if "\t" in ln else ln.split(","))]
        parsed.append(cols)
    bio = generate_invoice_xlsx(os.getenv("COMPANY_NAME", "公司名称"),
                                customer or "客户", bankinfo or "", parsed)
    filename = f"invoice_{int(time.time())}.xlsx"
    return send_file(bio, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ------------------------------
# 启动准备
# ------------------------------
def ensure_admin_user():
    try:
        if User.query.count() == 0:
            admin_name = os.getenv("ADMIN_USER")
            admin_pass = os.getenv("ADMIN_PASS")
            if not admin_name or not admin_pass:
                app.logger.warning("未配置管理员凭据，跳过创建用户")
                return
            u = User(username=admin_name, is_admin=True)
            u.set_password(admin_pass)
            db.session.add(u)
            db.session.commit()
            app.logger.info("已创建管理员账号")
    except Exception:
        app.logger.exception("创建默认管理员失败")

# ------------------------------
# 主入口
# ------------------------------
if __name__ == "__main__":
    raise SystemExit("Use Gunicorn for deployment or dev_local.py for local demo.")

flask_app = app
application = app
