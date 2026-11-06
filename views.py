from urllib.parse import urlsplit
from tracking import parse_response, QueryStatus
from flask import Blueprint, request, render_template, redirect, url_for, flash, send_file, current_app, abort
from models import db, CarrierAgent, Shipment, Customer, BankAccount, ManualTrack
from flask_login import login_required, logout_user, current_user
from sqlalchemy.orm import joinedload, selectinload
from datetime import datetime, timedelta
from sqlalchemy import inspect as sqlalchemy_inspect, MetaData, Table, select, func
from utils import export_invoice
import requests
import carrier_http
import json
import os
import time

views = Blueprint("views", __name__)

# -------------------------------
# API 轨迹获取函数
# -------------------------------
def fetch_tracking_from_api(agent: CarrierAgent, tracking_number: str, *, selected_shipment=None, safe_errors=False, read_only_preview=False):
    """
    根据不同的代理调用不同 API
    """
    if selected_shipment is not None and (
        selected_shipment.tracking_number != tracking_number
        or not agent or selected_shipment.agent_id != agent.id
    ):
        return None, "运单身份与代理不一致"
    if not agent or not agent.api_url or not agent.supports_api or not agent.is_active:
        return None, "代理不支持 API 抓取"

    try:
        host = (urlsplit(agent.api_url).hostname or "").rstrip(".").lower()
    except ValueError:
        return None, "代理 API 地址无效"
    is_rtb56 = host == "rtb56.com" or host.endswith(".rtb56.com")
    if selected_shipment is not None and is_rtb56:
        if not selected_shipment.tracking_number or not selected_shipment.tracking_number.strip():
            return None, "RTB56 查询单号缺失"
        # The supplier sees the agent account and number, not our local primary key.
        if Shipment.query.filter_by(agent_id=agent.id, tracking_number=tracking_number).filter(
                Shipment.id != selected_shipment.id).first() is not None:
            return None, "同一代理存在重复查询单号，无法核对 RTB56 运单身份"
        # Restore the legacy request number, then require the supplier to echo it.
        try:
            payload = {
                "appToken": agent.app_token or "",
                "appKey": agent.app_key or "",
                "serviceMethod": "gettrack",
                "paramsJson": json.dumps(
                    {"tracking_number": selected_shipment.tracking_number}, ensure_ascii=False),
            }
            response = carrier_http.post(agent, data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
            result = parse_response("rtb56", str(agent.id), response.status_code,
                                    response.text, expected_number=selected_shipment.tracking_number,
                                    allow_unverified_preview=(read_only_preview is True and not safe_errors))
            if (result.status == QueryStatus.UNVERIFIED
                    and result.message == "rtb56:identity_unverified_preview"
                    and result.events and read_only_preview is True and not safe_errors):
                current_app.logger.warning("rtb56 category=identity_unverified_preview")
                return [{"time": event.time.raw or "", "location": event.location,
                         "description": event.description} for event in result.events], (
                    "服务商单号尚未验证归属。以下外部轨迹仅供只读预览，不用于同步或物流状态更新。")
            diagnostic = None
            if result.status == QueryStatus.EMPTY:
                diagnostic = "empty_details"
            elif result.status != QueryStatus.SUCCESS:
                if result.error and result.error.value == "http":
                    diagnostic = "http_error"
                elif result.message and result.message.startswith("rtb56:"):
                    diagnostic = result.message[len("rtb56:"):]
                else:
                    diagnostic = {"invalid_json": "invalid_json", "schema": "structure_or_event_error",
                                  "business": "supplier_failure"}.get(
                                      result.error.value if result.error else "", "parse_error")
            if diagnostic:
                status_code = response.status_code if type(response.status_code) is int else "invalid_type"
                current_app.logger.warning("rtb56 category=%s http_status=%s", diagnostic, status_code)
            if result.status not in (QueryStatus.SUCCESS, QueryStatus.EMPTY):
                return None, "RTB56 查询失败 [" + diagnostic + "]；单号身份未通过验证或响应无法确认"
            return [{"time": event.time.raw or "", "location": event.location,
                     "description": event.description} for event in result.events], None
        except Exception as error:
            # The transport exposes only this fixed non-200 category, not the numeric status.
            category = ("http_error_status_unavailable"
                        if type(error) is carrier_http.CarrierRequestError
                        and str(error) == "代理 API 返回非成功状态" else "transport_or_processing_error")
            current_app.logger.warning("rtb56 category=%s", category)
            return None, ("tracking:request_failed" if safe_errors else
                          "API请求失败 [" + category + "]，请检查代理配置或稍后重试")

    try:
        # ========================
        # 1. YWTYGJ 接口 (rtb56) 和 txfba.com 接口
        # ========================
        if "rtb56.com" in (agent.api_url or "") or "txfba.com" in (agent.api_url or ""):
            payload = {
                "appToken": agent.app_token,
                "appKey": agent.app_key,
                "serviceMethod": "gettrack",
                "paramsJson": json.dumps({"tracking_number": tracking_number})
            }
            
            print("tracking:request")
            print("物流操作完成或失败；详细数据不写入日志")
            
            resp = carrier_http.post(agent, data=payload, timeout=15)
            print("物流操作完成或失败；详细数据不写入日志")

            if resp.status_code != 200:
                return None, f"API返回错误 {resp.status_code}"

            data = resp.json()
            if not data.get("success"):
                # 返回的 message 在 cnmessage/enmessage
                return None, "tracking:business_error" if safe_errors else (data.get("cnmessage") or data.get("enmessage") or "接口调用失败")

            # 轨迹在 data[0]["details"]
            tracks = []
            if data.get("data") and isinstance(data["data"], list):
                details = data["data"][0].get("details", [])
                for d in details:
                    tracks.append({
                        "time": d.get("track_occur_date"),
                        "location": d.get("track_location"),
                        "description": d.get("track_description")
                    })
            return tracks, None

        # ========================
        # 2. NEXTSLS 接口 (nextsls)
        #    使用 shipment_id 去查询
        # ========================
        elif "nextsls.com" in (agent.api_url or ""):
            # 根据 tracking_number 获取对应的 shipment
            shipment = (selected_shipment if selected_shipment is not None else
                        Shipment.query.filter_by(tracking_number=tracking_number).first())
            
            # 修复：处理shipment_id缺失的情况
            if not shipment:
                # 尝试直接使用tracking_number作为shipment_id
                shipment_id = tracking_number
                print("物流操作状态已记录（不包含业务数据）")
            elif not shipment.shipment_id:
                # 有shipment记录但没有shipment_id，使用tracking_number
                shipment_id = tracking_number
                print("物流操作状态已记录（不包含业务数据）")
            else:
                shipment_id = shipment.shipment_id

            url = agent.api_url
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {agent.app_token}"  # token 存在 app_token 字段
            }
            payload = {
                "shipment": {
                    "shipment_id": shipment_id,  # 使用 shipment_id 或 tracking_number
                    "language": "zh"
                }
            }
            resp = carrier_http.post(agent, headers=headers, json=payload, timeout=15)
            print("物流操作完成或失败；详细数据不写入日志")

            if resp.status_code != 200:
                return None, f"API返回错误 {resp.status_code}"

            data = resp.json()
            if data.get("status") != 1:
                return None, "tracking:business_error" if safe_errors else data.get("info", "接口调用失败")

            traces = data.get("data", {}).get("shipment", {}).get("traces", [])
            tracks = []
            for t in traces:
                ts = t.get("time")
                if isinstance(ts, int):
                    ts = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                tracks.append({
                    "time": ts,
                    "location": t.get("location", ""),
                    "description": t.get("info", "")
                })
            return tracks, None

        # ========================
        # 3. 默认情况（统一使用POST请求）
        # ========================
        else:
            # 统一使用POST请求，支持更多接口
            payload = {
                "appKey": agent.app_key,
                "appToken": agent.app_token,
                "tracking_number": tracking_number
            }
            
            print("tracking:request")
            print("物流操作完成或失败；详细数据不写入日志")
            
            resp = carrier_http.post(agent, data=payload, timeout=10)
            print("物流操作完成或失败；详细数据不写入日志")

            if resp.status_code != 200:
                return None, f"API返回错误 {resp.status_code}"

            data = resp.json()
            # 尝试多种常见字段
            if isinstance(data, dict):
                if "tracks" in data:
                    return data["tracks"], None
                if "data" in data and data["data"]:
                    return data["data"], None
                if "result" in data and isinstance(data["result"], dict) and "list" in data["result"]:
                    return data["result"]["list"], None
                # 添加对错误信息的处理
                if "code" in data and data["code"] != 200:
                    return None, "tracking:business_error" if safe_errors else data.get("message", "接口返回错误")
            return None, "未找到轨迹字段"

    except Exception as e:
        return None, "tracking:request_failed" if safe_errors else "API请求失败，请检查代理配置或稍后重试"


# =============== 新增：去重同步函数 ===============
def sync_tracking_to_supabase(shipment, tracks):
    if not _sync_identity_safe(shipment):
        return 0, 1
    """
    同步轨迹数据到Supabase，避免重复数据
    """
    try:
        supabase_url = os.environ.get('SUPABASE_URL')
        supabase_key = os.environ.get('SUPABASE_KEY')
        
        success_count = 0
        error_count = 0
        
        for track in tracks:
            try:
                # 生成唯一标识符，用于检查重复
                event_time = track.get('time')
                if not event_time:
                    event_time = datetime.utcnow().isoformat()
                
                location = track.get('location', '')
                description = track.get('description', track.get('info', track.get('status', '')))
                
                # 检查是否已存在相同记录
                check_response = requests.get(
                    f"{supabase_url}/rest/v1/shipment_tracking_details",
                    params={
                        "tracking_number": f"eq.{shipment.tracking_number}",
                        "event_time": f"eq.{event_time}",
                        "description": f"eq.{description}",
                        "select": "id"
                    },
                    headers={
                        "Authorization": f"Bearer {supabase_key}",
                        "apikey": supabase_key
                    },
                    timeout=5
                )
                
                # 如果记录已存在，跳过插入
                if check_response.status_code == 200 and len(check_response.json()) > 0:
                    print("物流操作完成或失败；详细数据不写入日志")
                    continue
                
                track_data = {
                    "tracking_number": shipment.tracking_number,
                    "event_time": event_time,
                    "location": location,
                    "description": description
                }
                
                # 插入新记录
                response = requests.post(
                    f"{supabase_url}/rest/v1/shipment_tracking_details",
                    headers={
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json",
                        "apikey": supabase_key,
                        "Prefer": "return=minimal"
                    },
                    data=json.dumps(track_data),
                    timeout=10
                )
                
                if response.status_code in [200, 201, 204]:
                    success_count += 1
                    print("物流操作完成或失败；详细数据不写入日志")
                else:
                    error_count += 1
                    print(f"❌ 轨迹同步失败: {response.status_code}")
                    
            except Exception as e:
                error_count += 1
                print("物流操作完成或失败；详细数据不写入日志")
        
        return success_count, error_count
        
    except Exception as e:
        print("物流操作完成或失败；详细数据不写入日志")
        return 0, 1


def _sync_identity_safe(shipment):
    """Legacy cloud schema only identifies a shipment by customer number."""
    if shipment.agent and not shipment.agent.is_active:
        return False
    return Shipment.query.filter_by(tracking_number=shipment.tracking_number).count() == 1


def _refresh_query(shipment, agent, *, history=False):
    if not agent or not agent.is_active or not agent.supports_api:
        return None, "refresh:inactive_or_unavailable_agent"
    if agent.id != shipment.agent_id or not _sync_identity_safe(shipment):
        return None, "refresh:ambiguous_identity"
    if history:
        return None, "refresh:history_mapping_unavailable"
    try:
        tracks, error = fetch_tracking_from_api(agent, shipment.tracking_number, selected_shipment=shipment, safe_errors=True)
        return (None, "refresh:supplier_failure") if error else (tracks, None)
    except Exception:
        return None, "refresh:query_failure"


# -------------------------------
# 代理管理
# -------------------------------
@views.route("/agents", methods=["GET", "POST"])
@login_required
def agents():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("代理名称不能为空", "danger")
            return redirect(url_for("views.agents"))

        if CarrierAgent.query.filter_by(name=name).count():
            flash('代理名称已存在，请编辑现有代理', 'danger')
            return redirect(url_for('views.agents'))
        a = CarrierAgent(
            name=name,
            api_url=request.form.get("api_url"),
            app_key=request.form.get("app_key"),
            app_token=request.form.get("app_token"),
            customer_code=request.form.get("customer_code"),
            is_active=True
        )
        db.session.add(a)
        db.session.commit()
        flash("代理已添加", "success")
        return redirect(url_for("views.agents"))

    data = CarrierAgent.query.filter_by(is_active=True).all()
    return render_template("agent.html", agents=data)


@views.route("/agents/<int:agent_id>/edit", methods=["GET", "POST"])
@login_required
def edit_agent(agent_id):
    a = CarrierAgent.query.get_or_404(agent_id)
    if request.method == "POST":
        name = request.form.get('name', a.name).strip()
        if not name or CarrierAgent.query.filter(CarrierAgent.name == name, CarrierAgent.id != a.id).count():
            return '代理名称为空或已存在', 400
        a.name = name
        a.api_url = request.form.get("api_url", a.api_url)
        a.app_key = request.form.get("app_key", "").strip() or a.app_key
        a.app_token = request.form.get("app_token", "").strip() or a.app_token
        a.customer_code = request.form.get("customer_code", a.customer_code)
        db.session.commit()
        flash("代理已更新", "success")
        return redirect(url_for("views.agents"))

    return render_template("edit_agent.html", agent=a)


@views.route("/agents/<int:agent_id>/delete", methods=["POST"])
@login_required
def delete_agent(agent_id):
    from deletion import deactivate_agent
    from sqlalchemy.exc import SQLAlchemyError
    a = CarrierAgent.query.get_or_404(agent_id)
    try:
        deactivate_agent(a)
    except SQLAlchemyError:
        return "停用失败，全部修改已回滚", 409
    flash("代理已停用，历史运单关联保留", "success")
    return redirect(url_for("views.agents"))


# -------------------------------
# 运单管理
# -------------------------------
@views.route("/shipments")
@login_required
def shipments():
    query = Shipment.query.order_by(Shipment.created_at.desc(), Shipment.id.desc())

    # 从 URL 获取查询参数
    customer = request.args.get("customer")
    agent = request.args.get("agent")
    tracking_number = request.args.get("tracking_number")

    if customer:
        query = query.join(Customer).filter(Customer.name.ilike(f"%{customer}%"))
    if agent:
        query = query.join(CarrierAgent).filter(CarrierAgent.name.ilike(f"%{agent}%"))
    if tracking_number:
        query = query.filter(db.or_(Shipment.tracking_number.ilike(f"%{tracking_number.strip()}%"), Shipment.shipment_id.ilike(f"%{tracking_number.strip()}%")))

    page = request.args.get('page', 1, type=int)
    if not 1 <= page <= 2**31 - 1:
        abort(404)
    pagination = query.options(joinedload(Shipment.customer), joinedload(Shipment.agent)).paginate(
        page=page, per_page=50
    )
    filters = {key: value for key, value in (
        ('customer', customer), ('agent', agent), ('tracking_number', tracking_number)
    ) if value}
    return render_template("shipments.html", shipments=pagination.items,
                           pagination=pagination, filters=filters)


@views.route("/shipments/add", methods=["GET", "POST"])
@login_required
def add_shipment():
    if request.method == "POST":
        tracking_number = request.form.get("tracking_number", "").strip()
        import math
        try:
            if not tracking_number:
                raise ValueError()
            pieces = int(request.form.get('pieces') or 1)
            if pieces <= 0:
                raise ValueError()
            for field in ('weight', 'unit_price', 'surcharge_extra', 'operation_fee', 'high_value_fee'):
                value = float(request.form.get(field) or 0)
                if not math.isfinite(value) or value < 0:
                    raise ValueError()
            for field, model in (('customer_id', Customer), ('agent_id', CarrierAgent)):
                if request.form.get(field) and db.session.get(model, int(request.form[field])) is None:
                    raise ValueError()
        except (ValueError, TypeError, OverflowError):
            return '运单号、件数、费用或客户代理选择无效', 400
        if Shipment.query.filter_by(tracking_number=tracking_number).first():
            flash(f"运单号 {tracking_number} 已存在，请更换", "danger")
            return redirect(url_for("views.add_shipment"))

        shipment = Shipment(
            tracking_number=tracking_number,
            shipment_id=request.form.get("shipment_id"),
            third_party_tracking_number=request.form.get("third_party_tracking_number"),
            customer_id=request.form.get("customer_id") or None,
            agent_id=request.form.get("agent_id") or None,
            carrier_id=request.form.get("carrier_id"),
            origin=request.form.get("origin"),
            destination=request.form.get("destination"),
            channel=request.form.get("channel"),
            product_type=request.form.get("product_type"),
            pieces=int(request.form.get("pieces") or 1),
            weight=float(request.form.get("weight") or 0),
            unit_price=float(request.form.get("unit_price") or 0),
            surcharge_extra=float(request.form.get("surcharge_extra") or 0),
            operation_fee=float(request.form.get("operation_fee") or 0),
            high_value_fee=float(request.form.get("high_value_fee") or 0),
            status="已录入",
            note=request.form.get("note")
        )
        shipment.fee = shipment.weight * shipment.unit_price + shipment.surcharge_extra + shipment.operation_fee + shipment.high_value_fee
        if not math.isfinite(shipment.fee):
            return '费用超出有效范围', 400

        db.session.add(shipment)
        db.session.commit()
        
        if current_app.config.get('ISOLATED_MODE'):
            flash('运单已保存（本地演示不执行外部同步）', 'success')
            return redirect(url_for('views.shipments'))

        # =============== 优化后的Supabase同步代码 ===============
        try:
            # 从环境变量获取Supabase配置（更安全）
            supabase_url = os.environ.get('SUPABASE_URL')
            supabase_key = os.environ.get('SUPABASE_KEY')
            # 获取客户名称
            customer_name = "未知客户"
            if shipment.customer_id:
                customer = Customer.query.get(shipment.customer_id)
                if customer:
                    customer_name = customer.name
            
            # 准备同步数据
            data_to_insert = {
                "tracking_number": shipment.tracking_number,
                "customer_name": customer_name,
                "current_location": shipment.origin or "仓库",
                "status": "pending",
                "notes": f"目的地: {shipment.destination} | 渠道: {shipment.channel} | 重量: {shipment.weight}kg"
            }
            
            # 打印调试信息
            print("物流操作状态已记录（不包含业务数据）")
            
            response = requests.post(
                f"{supabase_url}/rest/v1/shipment_tracking",
                headers={
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type": "application/json",
                    "apikey": supabase_key,
                    "Prefer": "return=minimal"
                },
                data=json.dumps(data_to_insert),
                timeout=10  # 添加超时
            )
            
            # 详细的响应处理
            if response.status_code in [200, 201, 204]:
                print("物流操作状态已记录（不包含业务数据）")
                flash("运单已保存并同步到查询系统", "success")
            else:
                error_msg = f"❌ 同步失败: {response.status_code} - {response.text}"
                print("物流操作完成或失败；详细数据不写入日志")
                flash("运单已保存，但同步到查询系统失败", "warning")
                
        except requests.exceptions.Timeout:
            print("⏰ Supabase请求超时")
            flash("运单已保存，但同步到查询系统超时", "warning")
        except requests.exceptions.ConnectionError:
            print("🔌 网络连接错误")
            flash("运单已保存，但无法连接到查询系统", "warning")
        except Exception as e:
            error_msg = f"🔥 同步异常: {str(e)}"
            print("物流操作完成或失败；详细数据不写入日志")
            import traceback
            print("操作失败；详细业务数据不写入日志")
            flash("运单已保存，但同步到查询系统时出现异常", "warning")
        # =============== 同步代码结束 ===============
        
        # =============== 修改：轨迹同步代码 ===============
        try:
            # 只有有代理的运单才获取轨迹
            if shipment.agent_id:
                agent = CarrierAgent.query.get(shipment.agent_id)
                if agent and agent.supports_api:
                    print("物流操作状态已记录（不包含业务数据）")
                    
                    # 调用您的轨迹API获取函数
                    tracks, error = _refresh_query(shipment, agent)
                    
                    if tracks and not error:
                        print(f"✅ 获取到 {len(tracks)} 条轨迹信息")
                        
                        # 使用优化后的同步函数
                        success_count, error_count = sync_tracking_to_supabase(shipment, tracks)
                        print(f"📊 轨迹同步结果: 成功 {success_count}, 失败 {error_count}")
                    else:
                        print(f"⚠️ 无法获取轨迹: {error}")
                        
        except Exception as e:
            print("物流操作完成或失败；详细数据不写入日志")
            import traceback
            print("操作失败；详细业务数据不写入日志")
        # =============== 轨迹同步结束 ===============
        
        return redirect(url_for("views.shipments"))

    customers = Customer.query.all()
    agents = CarrierAgent.query.all()
    destinations = ["美国", "加拿大", "英国", "德国"]

    return render_template("add_shipment.html", customers=customers, agents=agents, destinations=destinations)


@views.route("/shipments/edit/<int:shipment_id>", methods=["GET", "POST"])
@login_required
def edit_shipment(shipment_id):
    s = Shipment.query.get_or_404(shipment_id)
    if request.method == "POST":
        from shipment_updates import update_shipment
        try:
            update_shipment(s, request.form)
        except (ValueError, TypeError):
            db.session.rollback()
            flash("运单号重复、为空或数字格式无效", "danger")
            return redirect(url_for("views.edit_shipment", shipment_id=shipment_id))

        db.session.commit()
        flash("运单已更新", "success")
        return redirect(url_for("views.shipments"))

    customers = Customer.query.all()
    agents = CarrierAgent.query.all()
    destinations = ["美国", "加拿大", "英国", "德国"]

    return render_template("add_shipment.html", shipment=s, customers=customers, agents=agents, destinations=destinations)


@views.route("/shipments/delete/<int:shipment_id>", methods=["POST"])
@login_required
def delete_shipment(shipment_id):
    from deletion import delete_records
    from sqlalchemy.exc import SQLAlchemyError
    s = Shipment.query.get_or_404(shipment_id)
    try:
        delete_records([s])
    except SQLAlchemyError:
        return "删除失败，全部修改已回滚", 409
    flash("运单及手工轨迹已删除；客户、代理和收款账户保留", "success")
    return redirect(url_for("views.shipments"))


# -------------------------------
# 银行账户管理
# -------------------------------
@views.route("/bank_accounts", methods=["GET", "POST"])
@login_required
def bank_accounts():
    if request.method == "POST":
        values, error = _validated_bank_account_form()
        if error:
            flash(error, "danger")
            return redirect(url_for("views.bank_accounts"))
        try:
            acc = BankAccount(**values)
            db.session.add(acc)
            _apply_default_bank_account(acc, values.pop("is_default", False))
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("银行账户保存失败，请检查填写内容", "danger")
            return redirect(url_for("views.bank_accounts"))
        flash("银行账户已添加", "success")
        return redirect(url_for("views.bank_accounts"))

    accounts = BankAccount.query.order_by(BankAccount.id.asc()).all()
    return render_template("bank_accounts.html", accounts=accounts)


def _validated_bank_account_form():
    """Validate account fields without coercing account numbers to integers."""
    values = {
        "account_type": request.form.get("account_type", "private").strip(),
        "bank_name": request.form.get("bank_name", "").strip(),
        "account_name": request.form.get("account_name", "").strip(),
        "account_no": request.form.get("account_no", "").strip(),
        "remark": request.form.get("remark", "").strip() or None,
    }
    if values["account_type"] not in {"private", "public"}:
        return None, "账户类型无效"
    limits = {"bank_name": 200, "account_name": 200, "account_no": 200, "remark": 500}
    if any(not values[field] for field in ("bank_name", "account_name", "account_no")):
        return None, "开户行、户名和账号不能为空"
    if any(values[field] and len(values[field]) > limit for field, limit in limits.items()):
        return None, "填写内容超出长度限制"
    values["is_default"] = request.form.get("is_default") in {"1", "true", "on"}
    return values, None


def _apply_default_bank_account(account, make_default):
    if make_default:
        BankAccount.query.filter(BankAccount.id != account.id).update(
            {BankAccount.is_default: False}, synchronize_session=False)
    account.is_default = bool(make_default)


def _has_referencing_rows(target_table, target_id):
    """Check actual FK references, including legacy tables not mapped as ORM models."""
    inspector = sqlalchemy_inspect(db.engine)
    metadata = MetaData()
    for table_name in inspector.get_table_names():
        if table_name == target_table:
            continue
        for foreign_key in inspector.get_foreign_keys(table_name):
            if foreign_key.get("referred_table") != target_table:
                continue
            for local_column, remote_column in zip(
                    foreign_key.get("constrained_columns") or (),
                    foreign_key.get("referred_columns") or ()):
                if remote_column != "id":
                    continue
                table = Table(table_name, metadata, autoload_with=db.engine)
                count = db.session.execute(
                    select(func.count()).select_from(table).where(table.c[local_column] == target_id)
                ).scalar_one()
                if count:
                    return True
    return False


@views.route("/bank_accounts/<int:account_id>/edit", methods=["GET", "POST"])
@login_required
def edit_bank_account(account_id):
    account = BankAccount.query.get_or_404(account_id)
    if request.method == "POST":
        values, error = _validated_bank_account_form()
        if error:
            return error, 400
        make_default = values.pop("is_default")
        for field, value in values.items():
            setattr(account, field, value)
        _apply_default_bank_account(account, make_default)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("银行账户保存失败，请检查填写内容", "danger")
            return redirect(url_for("views.edit_bank_account", account_id=account_id))
        flash("银行账户已更新", "success")
        return redirect(url_for("views.bank_accounts"))
    return render_template("edit_bank_account.html", account=account)


@views.route("/bank_accounts/<int:account_id>/delete", methods=["POST"])
@login_required
def delete_bank_account(account_id):
    account = BankAccount.query.get_or_404(account_id)
    if _has_referencing_rows("bank_account", account.id):
        flash("该账户已被历史记录引用，不能删除", "danger")
        return redirect(url_for("views.bank_accounts"))
    db.session.delete(account)
    db.session.commit()
    flash("银行账户已删除", "success")
    return redirect(url_for("views.bank_accounts"))


@views.route("/bank_accounts/<int:account_id>/default", methods=["POST"])
@login_required
def set_default_bank_account(account_id):
    account = BankAccount.query.get_or_404(account_id)
    _apply_default_bank_account(account, True)
    db.session.commit()
    flash("默认收款账户已更新", "success")
    return redirect(url_for("views.bank_accounts"))


@views.route("/shipments/<int:shipment_id>/tracks", methods=["GET", "POST"])
@login_required
def manual_tracks(shipment_id):
    shipment = Shipment.query.get_or_404(shipment_id)
    if request.method == "POST":
        description = request.form.get('description', '').strip()
        if not description:
            return '轨迹描述不能为空', 400
        track = ManualTrack(
            shipment_id=shipment.id,
            description=description,
            location=request.form.get("location"),
            happen_time=datetime.utcnow(),
            author=current_user.username
        )
        db.session.add(track)
        db.session.commit()
        
        if current_app.config.get('ISOLATED_MODE'):
            flash('轨迹已保存（本地演示不执行外部同步）', 'success')
            return redirect(url_for('views.manual_tracks', shipment_id=shipment.id))

        # =============== 新增代码：同步手工轨迹到Supabase ===============
        try:
            # 更新Supabase中的轨迹信息
            supabase_url = os.environ.get('SUPABASE_URL')
            supabase_key = os.environ.get('SUPABASE_KEY')
            
            # 更新当前位置和状态
            update_data = {
                "current_location": request.form.get("location"),
                "status": "in_transit",  # 手工添加轨迹通常表示运输中
                "notes": f"手工更新: {request.form.get('description')}",
                "updated_at": datetime.utcnow().isoformat()
            }
            
            response = requests.patch(
                f"{supabase_url}/rest/v1/shipment_tracking?tracking_number=eq.{shipment.tracking_number}",
                headers={
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type": "application/json",
                    "apikey": supabase_key,
                    "Prefer": "return=minimal"
                },
                data=json.dumps(update_data)
            )
            
            if response.status_code in [200, 201, 204]:
                print("物流操作状态已记录（不包含业务数据）")
            else:
                print(f"手工轨迹同步失败: {response.status_code}")
                
        except Exception as e:
            print("物流操作完成或失败；详细数据不写入日志")
        # =============== 新增代码结束 ===============
        
        flash("轨迹已添加", "success")
        return redirect(url_for("views.manual_tracks", shipment_id=shipment.id))

    events = ManualTrack.query.filter_by(shipment_id=shipment.id).order_by(ManualTrack.happen_time.desc()).all()
    return render_template("shipment_events.html", shipment=shipment, events=events)


# -------------------------------
# 客户管理
# -------------------------------
@views.route("/customers", methods=["GET", "POST"])
@login_required
def customers():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        bankinfo = request.form.get("bankinfo", "").strip()
        email = request.form.get("email", "").strip() or None
        phone = request.form.get("phone", "").strip() or None
        if not name:
            flash("客户名称不能为空", "danger")
            return redirect(url_for("views.customers"))

        if len(name) > 100 or (email and len(email) > 120) or (phone and len(phone) > 50):
            flash("客户信息超出长度限制", "danger")
            return redirect(url_for("views.customers"))
        if Customer.query.filter_by(name=name).first():
            flash('客户名称已存在，请使用现有客户', 'danger')
            return redirect(url_for('views.customers'))
        if email and Customer.query.filter_by(email=email).first():
            flash("客户邮箱已被其他客户使用", "danger")
            return redirect(url_for("views.customers"))
        c = Customer(name=name, bank_info=bankinfo, email=email, phone=phone)
        db.session.add(c)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("客户信息保存失败，请检查填写内容", "danger")
            return redirect(url_for("views.customers"))
        flash("客户已添加", "success")
        return redirect(url_for("views.customers"))

    data = Customer.query.all()
    return render_template("customers.html", customers=data)


@views.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
def edit_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip() or None
        phone = request.form.get("phone", "").strip() or None
        bank_info = request.form.get("bankinfo", "").strip() or None
        if not name:
            return "客户名称不能为空", 400
        duplicate = Customer.query.filter(Customer.name == name, Customer.id != customer.id).first()
        if duplicate:
            return "客户名称已存在", 400
        if email and Customer.query.filter(Customer.email == email, Customer.id != customer.id).first():
            return "客户邮箱已被其他客户使用", 400
        if len(name) > 100 or (email and len(email) > 120) or (phone and len(phone) > 50):
            return "填写内容超出长度限制", 400
        customer.name, customer.email, customer.phone, customer.bank_info = name, email, phone, bank_info
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return "客户信息保存失败，请检查填写内容", 400
        flash("客户信息已更新", "success")
        return redirect(url_for("views.customers"))
    return render_template("edit_customer.html", customer=customer)


@views.route("/customers/<int:customer_id>/delete", methods=["POST"])
@login_required
def delete_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    if _has_referencing_rows("customer", customer.id):
        flash("该客户已关联运单或历史业务记录，不能删除", "danger")
        return redirect(url_for("views.customers"))
    db.session.delete(customer)
    db.session.commit()
    flash("客户已删除", "success")
    return redirect(url_for("views.customers"))


# -------------------------------
# 财务模块
# -------------------------------
@views.route("/finance", methods=["GET", "POST"])
@login_required
def finance():
    customers = Customer.query.all()
    accounts = BankAccount.query.order_by(BankAccount.id.asc()).all()

    selected_customer_id = request.form.get("customer_id")
    raw_account_id = request.form.get("bank_account_id")
    default_accounts = [account for account in accounts if account.is_default]
    selected_account_id = raw_account_id or (
        str(default_accounts[0].id) if len(default_accounts) == 1 else "")
    date_from = request.form.get("date_from")
    date_to = request.form.get("date_to")

    query = Shipment.query
    if selected_customer_id:
        query = query.filter_by(customer_id=selected_customer_id)
    try:
        start_date = datetime.strptime(date_from, "%Y-%m-%d") if date_from else None
        end_date = datetime.strptime(date_to, "%Y-%m-%d") if date_to else None
        if start_date and end_date and start_date > end_date:
            raise ValueError("日期范围颠倒")
        if start_date:
            query = query.filter(Shipment.created_at >= start_date)
        if end_date:
            query = query.filter(Shipment.created_at < end_date + timedelta(days=1))
    except (ValueError, OverflowError):
        return "日期范围无效", 400

    shipments = query.all()
    total_fees = sum(s.fee or 0 for s in shipments)
    total_shipments = len(shipments)

    if request.form.get("export"):
        if not selected_account_id:
            return "请先选择收款银行账户", 400
        if not selected_account_id.isdigit():
            return "收款银行账户无效", 400
        selected_account = BankAccount.query.get(int(selected_account_id))
        if selected_account is None:
            return "收款银行账户无效", 400
        customer_name = "全部客户"
        if selected_customer_id:
            customer = Customer.query.get(selected_customer_id)
            customer_name = customer.name if customer else "未知客户"

        orders = []
        for s in shipments:
            orders.append({
                "date": s.created_at.strftime("%Y-%m-%d") if s.created_at else "",
                "order_no": s.tracking_number,
                "service_no": s.shipment_id or "",
                "qty": s.pieces,
                "type": s.product_type or "",
                "weight": s.weight,
                "destination": s.destination or "",
                "channel": s.channel or "",
                "fee": s.fee,
                "summary": s.note or ""
            })

        company_info = {
            "公司名称": "物流公司",
            "地址": ""
        }

        # 传递 date_from/date_to 给导出函数
        filename = export_invoice(company_info, customer_name, orders, [selected_account],
                                  date_from=date_from or None, date_to=date_to or None, filename=None)
        return send_file(filename, as_attachment=True, download_name="invoice.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    return render_template("finance.html",
                           customers=customers,
                           accounts=accounts,
                           shipments=shipments,
                           total_shipments=total_shipments,
                           total_fees=total_fees,
                           selected_customer_id=selected_customer_id,
                           selected_account_id=selected_account_id,
                           date_from=date_from,
                           date_to=date_to)


def _select_tracking_shipments(numbers, customer_id, agent_id):
    """一次批量匹配并预加载关系；歧义保留为逐票错误，不重新选票。"""
    query = Shipment.query
    if customer_id:
        query = query.filter_by(customer_id=customer_id)
    if agent_id:
        query = query.filter_by(agent_id=agent_id)

    # Resolve ambiguity before loading related rows for uniquely matched shipments.
    loaded = query.options(joinedload(Shipment.customer), joinedload(Shipment.agent),
                           selectinload(Shipment.manual_tracks))
    selections = []
    if not numbers:
        rows = loaded.limit(201).all()
        if len(rows) > 200:
            return None, '筛选结果超过 200 票，请缩小范围'
        selections = [(row.tracking_number, row, None) for row in rows]
    else:
        matches = {
            number: (count, row_id)
            for number, count, row_id in query.filter(Shipment.tracking_number.in_(numbers))
            .with_entities(Shipment.tracking_number, db.func.count(Shipment.id), db.func.min(Shipment.id))
            .group_by(Shipment.tracking_number).all()
        }
        unique_ids = [row_id for count, row_id in matches.values() if count == 1]
        rows_by_id = {row.id: row for row in loaded.filter(Shipment.id.in_(unique_ids)).all()} if unique_ids else {}
        for no in numbers:
            count, row_id = matches.get(no, (0, None))
            if count > 1:
                selections.append((no, None, "当前查询范围内运单号不唯一，请缩小筛选范围"))
            elif row_id not in rows_by_id or rows_by_id[row_id].tracking_number != no:
                selections.append((no, None, "当前查询范围内未找到运单"))
            else:
                selections.append((no, rows_by_id[row_id], None))
    return selections, None


def _tracking_result(s, no):
    """只处理已选定的一票：保留手工历史，外部失败不影响其他票。"""
    all_tracks = []
    err = None

    # 1. 手工轨迹（显示不带标签）
    if s:
        manual_events = s.manual_tracks
        if manual_events:
            for e in manual_events:
                all_tracks.append(f"{e.happen_time.strftime('%Y-%m-%d %H:%M')} {e.location or ''} {e.description}")

    # 2. API 轨迹（显示不带 [API] 标签）
    if s and s.agent:
        try:
            api_tracks, api_err = fetch_tracking_from_api(s.agent, no, selected_shipment=s, read_only_preview=True)
        except Exception:
            api_tracks, api_err = None, '该票物流查询失败，请稍后重试'
        if api_tracks:
            if isinstance(api_tracks, list):
                for ev in api_tracks:
                    if not isinstance(ev, dict):
                        err = '该票部分物流轨迹格式异常'
                        continue
                    time = ev.get("time") or ev.get("happen_time") or ""
                    loc = ev.get("location") or ""
                    desc = ev.get("description") or ev.get("status") or ""
                    all_tracks.append(f"{time} {loc} {desc}")
            elif isinstance(api_tracks, str):
                all_tracks.append(api_tracks)
        if api_err:
            err = api_err

    # 3. 如果都没有
    if not all_tracks:
        all_tracks.append("暂无轨迹")

    return {"tracking_number": no, "tracks": "\n".join(all_tracks), "error": err}


# -------------------------------
# 轨迹查询（合并手工 + API）
# -------------------------------
@views.route("/track", methods=["GET", "POST"])
@login_required
def track():
    if request.method == "POST":
        from tracking_integration import simulated_track
        simulated = simulated_track()
        if simulated is not None:
            results, message = simulated
            return render_template("track.html", agents=[], customers=[], carriers={},
                                   results=results, message=message, default_text="", public_mode=False)
    agents = CarrierAgent.query.filter_by(is_active=True).all()
    customers = Customer.query.all()
    carriers = {str(a.id): {"name": a.name} for a in agents}

    message, results, default_text = None, {}, ""

    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        agent_id = request.form.get("agent_id")
        numbers = list(dict.fromkeys(
            line.strip() for line in request.form.get('numbers', '').splitlines() if line.strip()
        ))
        if len(numbers) > 200:
            return '单次最多查询 200 个不同单号，请缩小范围', 400
        selections, selection_error = _select_tracking_shipments(numbers, customer_id, agent_id)
        if selection_error:
            return selection_error, 400
        if not selections:
            message = "未找到运单号"
        else:
            for index, (no, s, selection_error) in enumerate(selections):
                if selection_error:
                    results[f"input:{index}"] = {"tracking_number": no, "tracks": "暂无轨迹", "error": selection_error}
                    continue

                results[s.id] = _tracking_result(s, no)

            message = f"共查询到 {len(results)} 条结果"

    return render_template("track.html",
                           agents=agents,
                           customers=customers,
                           carriers=carriers,
                           results=results,
                           message=message,
                           default_text=default_text,
                           public_mode=False)


# -------------------------------
# 17Track 轨迹查询
# -------------------------------
@views.route("/track_17track", methods=["GET", "POST"])
@login_required
def track_17track():
    """
    17Track轨迹查询功能
    """
    message, results, default_text = None, {}, ""
    
    if request.method == "POST":
        numbers = request.form.get("numbers", "").strip().splitlines()
        
        if not numbers or numbers == [""]:
            message = "请输入要查询的运单号"
        else:
            for no in numbers:
                no = no.strip()
                if not no:
                    continue
                    
                # 调用17Track API进行查询
                tracks, err = fetch_17track_tracking(no)
                
                if err:
                    results[no] = {"tracks": "", "error": err}
                else:
                    results[no] = {"tracks": tracks, "error": None}
                    
            message = f"共查询到 {len(results)} 条结果"
    
    return render_template("track_17track.html",
                           results=results,
                           message=message,
                           default_text=default_text)


def fetch_17track_tracking(tracking_number):
    """
    调用17Track API获取轨迹信息
    """
    try:
        # 17Track API 调用逻辑
        # 这里需要根据17Track的实际API文档进行实现
        # 以下是示例代码，需要根据实际情况修改
        
        # 示例：使用17Track的API
        api_url = "https://api.17track.net/track/v2/gettrackinfo"
        headers = {
            "Content-Type": "application/json",
            "17token": "YOUR_17TRACK_API_TOKEN"  # 需要替换为实际的API token
        }
        
        payload = {
            "number": tracking_number,
            "carrier": None  # 自动识别快递公司
        }
        
        response = requests.post(api_url, headers=headers, json=payload, timeout=15)
        
        if response.status_code != 200:
            return None, f"API返回错误 {response.status_code}"
            
        data = response.json()
        
        # 解析17Track返回的数据
        if data.get("status") == 200 and data.get("data"):
            tracks = []
            for event in data["data"][0].get("track", []):
                tracks.append(f"{event.get('time')} {event.get('location')} {event.get('description')}")
            
            return "\n".join(tracks), None
        else:
            return None, data.get("message", "查询失败")
            
    except Exception as e:
        return None, f"API请求失败: {str(e)}"


# -------------------------------
# 登出
# -------------------------------
@views.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("您已登出", "success")
    return redirect(url_for("login"))

# -------------------------------
# 手动同步路由
# -------------------------------
@views.route("/admin/sync-to-supabase", methods=["POST"])
@login_required
def sync_to_supabase():
    """手动同步所有运单基本信息到Supabase"""
    try:
        supabase_url = os.getenv('SUPABASE_URL')
        supabase_key = os.getenv('SUPABASE_KEY')
        
        if not supabase_url or not supabase_key:
            flash("Supabase配置缺失", "danger")
            return redirect(url_for("views.shipments"))
        
        # 获取所有运单
        shipments = Shipment.query.all()
        success_count = 0
        error_count = 0
        
        for shipment in shipments:
            try:
                if not _sync_identity_safe(shipment):
                    error_count += 1
                    continue
                # 获取客户名称
                customer_name = "未知客户"
                if shipment.customer_id:
                    customer = Customer.query.get(shipment.customer_id)
                    if customer:
                        customer_name = customer.name
                
                # 准备数据
                data_to_insert = {
                    "tracking_number": shipment.tracking_number,
                    "customer_name": customer_name,
                    "current_location": shipment.origin or "仓库",
                    "status": "pending",
                    "notes": f"目的地: {shipment.destination} | 渠道: {shipment.channel}"
                }
                
                # 写入Supabase
                response = requests.post(
                    f"{supabase_url}/rest/v1/shipment_tracking",
                    headers={
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json",
                        "apikey": supabase_key,
                        "Prefer": "return=minimal"
                    },
                    data=json.dumps(data_to_insert),
                    timeout=10
                )
                
                if response.status_code in [200, 201, 204]:
                    success_count += 1
                    print("refresh:operation_status")
                else:
                    error_count += 1
                    print("refresh:operation_status")
                    
            except Exception as e:
                error_count += 1
                print("refresh:operation_status")
        
        flash(f"基本运单同步完成！成功: {success_count}, 失败: {error_count}", "success")
        
    except Exception as e:
        flash("refresh:operation_failed", "danger")
    
    return redirect(url_for("views.shipments"))


# =============== 修改：轨迹同步路由 ===============
@views.route("/admin/sync-tracking-details", methods=["POST"])
@login_required
def sync_tracking_details():
    """手动同步所有运单的轨迹信息（已优化去重）"""
    try:
        # 获取Supabase配置
        supabase_url = os.environ.get('SUPABASE_URL')
        supabase_key = os.environ.get('SUPABASE_KEY')
        
        print("refresh:operation_status")
        
        # 获取所有有代理的运单
        shipments = Shipment.query.filter(Shipment.agent_id.isnot(None)).all()
        total_success = 0
        total_error = 0
        
        print("refresh:operation_status")
        
        for shipment in shipments:
            try:
                agent = CarrierAgent.query.get(shipment.agent_id)
                if agent and agent.supports_api:
                    print("refresh:operation_status")
                    
                    # 调用轨迹API
                    tracks, error = _refresh_query(shipment, agent)
                    
                    print("refresh:operation_status")
                    
                    if tracks and not error:
                        # 使用优化后的同步函数
                        success_count, error_count = sync_tracking_to_supabase(shipment, tracks)
                        total_success += success_count
                        total_error += error_count
                        print("refresh:operation_status")
                    else:
                        print("refresh:operation_status")
                        total_error += 1
                else:
                    print("refresh:operation_status")
                    total_error += 1
                        
            except Exception as e:
                total_error += 1
                print("refresh:operation_status")
        
        print("refresh:operation_status")
        flash(f"轨迹同步完成！成功: {total_success}, 失败: {total_error}", "success")
        
    except Exception as e:
        print("refresh:operation_status")
        flash("refresh:operation_failed", "danger")
    
    return redirect(url_for("views.shipments"))

# -------------------------------
# 一键刷新物流轨迹
# -------------------------------
@views.route("/admin/refresh-tracking", methods=["POST"])
@login_required
def refresh_tracking():
    """严格内存防护的刷新函数"""
    try:
        import gc
        import time
        
        # 内存防护：只获取最近1天的3个运单
        one_day_ago = datetime.utcnow() - timedelta(days=1)
        shipments = Shipment.query.filter(
            Shipment.agent_id.isnot(None),
            Shipment.created_at >= one_day_ago
        ).limit(3).all()  # 限制最多3个运单
        
        updated_count = 0
        error_count = 0
        total_count = len(shipments)
        
        print("refresh:operation_status")
        
        for i, shipment in enumerate(shipments, 1):
            try:
                print("refresh:operation_status")
                
                agent = CarrierAgent.query.get(shipment.agent_id)
                if agent and agent.supports_api:
                    # 调用轨迹API
                    tracks, error = _refresh_query(shipment, agent)
                    
                    if tracks and not error:
                        # 简化同步
                        success_count = simple_sync_tracking(shipment, tracks)
                        
                        if success_count > 0:
                            updated_count += 1
                            print("refresh:operation_status")
                        else:
                            print("refresh:operation_status")
                    else:
                        error_count += 1
                        print("refresh:operation_status")
                else:
                    error_count += 1
                    print("refresh:operation_status")
                
                # 内存防护：处理完每个运单后强制清理
                gc.collect()
                
                # 内存防护：增加延迟，避免频繁请求
                if i < total_count:  # 最后一个不需要延迟
                    time.sleep(5)  # 更长的延迟
                    
            except Exception as e:
                error_count += 1
                print("refresh:operation_status")
                gc.collect()  # 出错时也清理内存
        
        flash(f"刷新完成！成功: {updated_count}/{total_count}, 失败: {error_count}", "success")
        
    except Exception as e:
        flash(f"刷新过程出错", "danger")
    
    return redirect(url_for("views.shipments"))


@views.route("/shipments/<int:shipment_id>/refresh", methods=["POST"])
@login_required
def refresh_single_tracking(shipment_id):
    """刷新单个运单的物流轨迹"""
    try:
        shipment = Shipment.query.get_or_404(shipment_id)
        
        if not shipment.agent_id:
            flash("该运单没有配置物流代理", "warning")
            return redirect(url_for("views.shipments"))
        
        agent = CarrierAgent.query.get(shipment.agent_id)
        if not agent or not agent.supports_api:
            flash("该运单的代理不支持API抓取", "warning")
            return redirect(url_for("views.shipments"))
        
        print("refresh:operation_status")
        
        # 调用轨迹API获取最新数据
        tracks, error = _refresh_query(shipment, agent)
        
        if tracks and not error:
            # 同步到Supabase
            success_count = simple_sync_tracking(shipment, tracks)
            
            if success_count > 0:
                flash(f"运单 {shipment.tracking_number} 更新成功，新增 {success_count} 条轨迹", "success")
            else:
                flash(f"运单 {shipment.tracking_number} 暂无新轨迹", "info")
        else:
            flash(f"获取运单 {shipment.tracking_number} 轨迹失败: {error}", "danger")
            
    except Exception as e:
        flash("refresh:operation_failed", "danger")
        import traceback
        print("refresh:operation_status")
    
    return redirect(url_for("views.shipments"))


def simple_sync_tracking(shipment, tracks):
    if not _sync_identity_safe(shipment):
        return 0
    """修复环境变量+内存溢出防护"""
    try:
        import requests
        import json
        import os
        import gc  # 添加垃圾回收
        
        # 使用正确的环境变量名称
        supabase_url = os.getenv('SUPABASE_URL')
        supabase_key = os.getenv('SUPABASE_ANON_KEY')
        
        print(f"🔧 环境变量检查:")
        print(f"   SUPABASE_URL: {supabase_url is not None}")
        print(f"   SUPABASE_ANON_KEY: {supabase_key is not None}")
        
        if not supabase_url or not supabase_key:
            print("❌ Supabase配置缺失 - 跳过该运单")
            return 0
        
        success_count = 0
        
        # 内存防护1：限制处理轨迹数量
        recent_tracks = tracks[:1] if tracks else []  # 只处理1条，避免内存溢出
        
        print(f"📦 处理 {len(recent_tracks)} 条轨迹 (严格限制数量)")
        
        for i, track in enumerate(recent_tracks):
            try:
                # 内存防护2：简化数据处理
                description = track.get('description') or track.get('info') or track.get('status') or '无描述'
                description = str(description)[:30]  # 更短的长度限制
                
                event_time = track.get('time') or '2025-01-01T00:00:00Z'
                location = track.get('location') or ''
                
                # 内存防护3：简化数据结构
                track_data = {
                    "tracking_number": shipment.tracking_number,
                    "event_time": event_time,
                    "location": location,
                    "description": description
                }
                
                print("物流操作完成或失败；详细数据不写入日志")
                
                # 内存防护4：使用更短的超时和简化的请求
                response = requests.post(
                    f"{supabase_url}/rest/v1/shipment_tracking_details",
                    headers={
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json", 
                        "apikey": supabase_key,
                        "Prefer": "return=minimal"
                    },
                    data=json.dumps(track_data),
                    timeout=3  # 更短的超时
                )
                
                # 内存防护5：简化响应处理
                if response.status_code in [200, 201, 204, 409]:
                    success_count += 1
                    print(f"✅ 写入成功")
                else:
                    print(f"⚠️ 写入异常: {response.status_code}")
                
                # 内存防护6：立即清理临时变量
                del track_data, description, event_time, location
                    
            except Exception as e:
                print(f"❌ 单条写入失败")
        
        # 内存防护7：强制垃圾回收
        gc.collect()
        
        print(f"🎯 完成: {success_count}/{len(recent_tracks)} 成功")
        return success_count
        
    except Exception as e:
        print(f"💥 写入过程出错")
        return 0
    
# -------------------------------
# 自动同步功能
# -------------------------------

def complete_sync_tracking(shipment, tracks):
    # No automatic Track model exists. Do not invent a persistence mapping.
    print("sync:history_mapping_unavailable")
    return 0


@views.route("/admin/refresh-full-history/<int:shipment_id>", methods=["POST"])
@login_required
def refresh_full_history(shipment_id):
    """强制刷新完整历史轨迹 - 解决历史数据缺失问题"""
    try:
        shipment = Shipment.query.get_or_404(shipment_id)
        
        if not shipment.agent_id:
            flash("该运单没有关联代理，无法刷新", "warning")
            return redirect(url_for("views.shipments"))
        
        agent = CarrierAgent.query.get(shipment.agent_id)
        if not agent:
            flash("找不到对应的代理", "warning")
            return redirect(url_for("views.shipments"))
        
        print("refresh:operation_status")
        
        # 获取完整轨迹数据
        tracks, error = _refresh_query(shipment, agent, history=True)
        if tracks:
            print("refresh:operation_status")
            
            # 显示所有获取到的轨迹
            for i, track in enumerate(tracks):
                time_str = track.get('track_time').strftime('%Y-%m-%d %H:%M:%S') if track.get('track_time') else '未知时间'
                print("refresh:operation_status")
            
            # 使用完整同步函数
            success_count = complete_sync_tracking(shipment, tracks)
            if success_count > 0:
                flash(f"✅ 完整历史轨迹刷新成功！添加了 {success_count} 条轨迹", "success")
                print("refresh:operation_status")
            else:
                flash("没有发现新的轨迹信息", "info")
        else:
            flash(f"获取轨迹失败: {error}", "danger")
            print("refresh:operation_status")
            
    except Exception as e:
        error_msg = "refresh:operation_failed"
        flash(error_msg, "danger")
        print("refresh:operation_status")
        import traceback
        print("refresh:operation_status")
    
    return redirect(url_for("views.shipments"))


@views.route("/admin/auto-sync-single/<int:shipment_id>", methods=["POST"])
@login_required
def auto_sync_single(shipment_id):
    """同步单个运单 - 使用完整同步"""
    try:
        shipment = Shipment.query.get_or_404(shipment_id)
        
        if not shipment.agent_id:
            flash("该运单没有关联代理，无法同步", "warning")
            return redirect(url_for("views.shipments"))
        
        agent = CarrierAgent.query.get(shipment.agent_id)
        if not agent or not agent.supports_api:
            flash("该代理不支持API同步", "warning")
            return redirect(url_for("views.shipments"))
        
        print("refresh:operation_status")
        
        tracks, error = _refresh_query(shipment, agent, history=True)
        if tracks and not error:
            print("refresh:operation_status")
            
            # 使用完整的轨迹同步函数
            success_count = complete_sync_tracking(shipment, tracks)
            if success_count > 0:
                flash(f"✅ 运单同步成功！添加了 {success_count} 条轨迹", "success")
                print("refresh:operation_status")
            else:
                flash("没有发现新的轨迹信息", "info")
        else:
            flash(f"同步失败: {error}", "danger")
            print("refresh:operation_status")
            
    except Exception as e:
        error_msg = "refresh:operation_failed"
        flash(error_msg, "danger")
        print("refresh:operation_status")
        import traceback
        print("refresh:operation_status")
    
    return redirect(url_for("views.shipments"))


@views.route("/admin/auto-sync-all-safe", methods=["POST"])
@login_required
def auto_sync_all_safe():
    """安全版本的全量同步 - 极低内存占用"""
    try:
        # 使用流式处理，避免一次性加载所有数据
        page = 1
        page_size = 5  # 减少每批数量
        total_updated = 0
        total_processed = 0
        max_process = 30  # 最大处理数量
        
        while total_processed < max_process:
            # 使用分页查询，只获取必要字段
            shipments_pagination = Shipment.query.filter(
                Shipment.agent_id.isnot(None)
            ).paginate(page=page, per_page=page_size, error_out=False)
            
            if not shipments_pagination.items:
                break
                
            print("refresh:operation_status")
            
            for shipment in shipments_pagination.items:
                try:
                    if total_processed >= max_process:
                        break
                        
                    total_processed += 1
                    print("refresh:operation_status")
                    
                    # 极简内存监控
                    try:
                        import psutil
                        memory_usage = psutil.virtual_memory().percent
                        if memory_usage > 70:  # 更严格的内存限制
                            print("refresh:operation_status")
                            flash(f"内存使用较高，已安全同步 {total_updated}/{total_processed} 个运单", "warning")
                            return redirect(url_for("views.shipments"))
                    except ImportError:
                        pass
                    
                    # 处理单个运单
                    agent = CarrierAgent.query.get(shipment.agent_id)
                    if agent and agent.supports_api:
                        tracks, error = _refresh_query(shipment, agent, history=True)
                        if tracks and not error:
                            success_count = complete_sync_tracking(shipment, tracks)
                            if success_count > 0:
                                total_updated += 1
                                print("refresh:operation_status")
                    
                    # 立即释放内存
                    import gc
                    gc.collect()
                    
                    # 更长的延迟，减少服务器压力
                    time.sleep(5)
                    
                except Exception as e:
                    print("refresh:operation_status")
                    continue
            
            page += 1
        
        flash(f"安全同步完成！更新 {total_updated}/{total_processed} 个运单", "success")
        
    except Exception as e:
        flash("refresh:operation_failed", "danger")
    
    return redirect(url_for("views.shipments"))


@views.route("/admin/auto-sync-all", methods=["POST"])
@login_required
def auto_sync_all():
    """自动同步所有运单 - 优化内存版本"""
    try:
        # 分批处理，避免一次性加载所有数据
        page = 1
        page_size = 10  # 减少每批数量
        total_updated = 0
        total_processed = 0
        
        while True:
            # 分批查询运单
            shipments = Shipment.query.filter(
                Shipment.agent_id.isnot(None)
            ).paginate(page=page, per_page=page_size, error_out=False)
            
            if not shipments.items:
                break
                
            print("refresh:operation_status")
            
            batch_updated = 0
            for i, shipment in enumerate(shipments.items, 1):
                try:
                    total_processed += 1
                    print("refresh:operation_status")
                    
                    # 内存监控
                    try:
                        import psutil
                        memory_usage = psutil.virtual_memory().percent
                        
                        if memory_usage > 80:  # 降低阈值
                            print("refresh:operation_status")
                            flash(f"内存使用过高，已同步 {total_updated}/{total_processed} 个运单", "warning")
                            return redirect(url_for("views.shipments"))
                    except ImportError:
                        # 如果没有安装psutil，跳过内存检查
                        pass
                    
                    agent = CarrierAgent.query.get(shipment.agent_id)
                    if agent and agent.supports_api:
                        tracks, error = _refresh_query(shipment, agent, history=True)
                        if tracks and not error:
                            # 使用完整同步函数
                            success_count = complete_sync_tracking(shipment, tracks)
                            if success_count > 0:
                                total_updated += 1
                                batch_updated += 1
                                print("refresh:operation_status")
                    
                    # 增加延迟
                    time.sleep(4)
                    
                    # 立即垃圾回收
                    import gc
                    gc.collect()
                        
                except Exception as e:
                    print("refresh:operation_status")
                    continue
            
            print("refresh:operation_status")
            page += 1
            
            # 限制总处理数量，防止无限循环
            if total_processed >= 50:  # 减少最大处理数量
                print("refresh:operation_status")
                break
        
        flash(f"自动同步完成！更新 {total_updated}/{total_processed} 个运单", "success")
        
    except Exception as e:
        flash("refresh:operation_failed", "danger")
    
    return redirect(url_for("views.shipments"))


@views.route("/admin/auto-sync-recent", methods=["POST"])
@login_required
def auto_sync_recent():
    """手动触发最近运单同步 - 优化内存版本"""
    try:
        # 同步最近7天的运单，进一步限制数量
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        shipments = Shipment.query.filter(
            Shipment.agent_id.isnot(None),
            Shipment.created_at >= seven_days_ago
        ).limit(8).all()  # 减少到8个
        
        updated_count = 0
        total_count = len(shipments)
        
        print("refresh:operation_status")
        
        for i, shipment in enumerate(shipments, 1):
            try:
                print("refresh:operation_status")
                
                agent = CarrierAgent.query.get(shipment.agent_id)
                if agent and agent.supports_api:
                    # 添加内存监控
                    try:
                        import psutil
                        memory_usage = psutil.virtual_memory().percent
                        
                        if memory_usage > 80:  # 降低阈值
                            print("refresh:operation_status")
                            flash(f"内存使用过高，已同步 {updated_count}/{i-1} 个运单", "warning")
                            break
                    except ImportError:
                        # 如果没有安装psutil，跳过内存检查
                        pass
                    
                    tracks, error = _refresh_query(shipment, agent, history=True)
                    if tracks and not error:
                        # 使用完整同步函数
                        success_count = complete_sync_tracking(shipment, tracks)
                        if success_count > 0:
                            updated_count += 1
                            print("refresh:operation_status")
                
                # 增加延迟，减少服务器压力
                time.sleep(4)
                
                # 立即垃圾回收
                import gc
                gc.collect()
                
            except Exception as e:
                print("refresh:operation_status")
                # 继续处理下一个运单，不中断整个流程
        
        flash(f"最近运单同步完成！更新 {updated_count}/{total_count} 个运单", "success")
        
    except Exception as e:
        flash("refresh:operation_failed", "danger")
    
    return redirect(url_for("views.shipments"))
