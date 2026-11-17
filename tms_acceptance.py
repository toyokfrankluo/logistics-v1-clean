"""Prepare traceable, explicitly labelled copies of an authorized sample. Never send shipments."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import re
from types import SimpleNamespace
from zipfile import ZipFile

from sqlalchemy import select, text
from models import CarrierAgent, db
from tms_models import TmsAgentApi, TmsAudit, TmsOrder, TmsReceivable
from tms_xlsx_images import invoice_sheet_part
from tms_invoice import parse_invoice, product_photos
import nextsls
import tms_carriers
import tms_supplier_images as images


def sample_copy(raw, reference):
    if not isinstance(reference,str) or not nextsls.CODE.fullmatch(reference):
        raise nextsls.ProtocolError("验收参考号格式无效。")
    output = BytesIO()
    with ZipFile(BytesIO(raw)) as original, ZipFile(output, "w") as copied:
        part = invoice_sheet_part(original)
        if not part:
            raise nextsls.ProtocolError("无法定位原发票工作表。")
        content = original.read(part).decode("utf-8")
        pattern = r'<(?P<tag>(?:[\w.-]+:)?c)\b(?P<attrs>[^>]*\br="D4"[^>]*)>.*?</(?P=tag)>'
        def replace(match):
            tag = match["tag"]
            prefix = tag.rsplit(":",1)[0]+":" if ":" in tag else ""
            attrs = re.sub(r'\s+t="[^"]*"', "", match["attrs"])
            return f'<{tag}{attrs} t="inlineStr"><{prefix}is><{prefix}t>{reference}</{prefix}t></{prefix}is></{tag}>'
        content, count = re.subn(pattern, replace, content, flags=re.S)
        if count != 1:
            raise nextsls.ProtocolError("原发票订单号单元格无法安全复制。")
        for entry in original.infolist():
            copied.writestr(entry, content.encode() if entry.filename == part else original.read(entry))
    result = output.getvalue()
    if parse_invoice(result)["customer_number"] != reference:
        raise nextsls.ProtocolError("验收副本订单号校验失败。")
    return result


def candidate(invoice, agent, profile, prefer_photos):
    options = []
    for code, name in tms_carriers.channel_catalogue(profile).items():
        if invoice["address"]["country"] == "US" and not re.search(r"美国|美西|美东|美森|\bUSA?\b", name, re.I):
            continue
        if re.search("危险|纯电|电池|敏感",name):
            continue
        rules = tms_carriers.channel_rules(profile, code)
        if rules is None:
            continue
        order = SimpleNamespace(invoice=invoice,agent_id=agent.id,agent_service=code,id=0)
        _, encoders = images.image_delivery(order,profile,rules)
        packing = images.media_store(order,profile) if rules["packing_list_required"] != 0 else None
        try:
            nextsls.create_payload(invoice,code,"XR-CASE-PREFLIGHT",rules,profile.sender,
                supplement=tms_carriers.order_supplement(order),image_fields=encoders,
                packing_host=packing.host if packing else "",include_packing=bool(packing),
                use_invoice_po=tms_carriers.invoice_po_allowed(profile,code),preflight=True,
                photos_protocol_confirmed=images.photos_encoder(profile,code) is not None)
        except nextsls.ProtocolError:
            continue
        photos = "photos" in nextsls.declaration_requirements(rules)
        options.append((0 if prefer_photos and photos else 1, len(rules.get("unknown_rules",[])),code,name,photos,rules))
    if not options:
        return None
    _,_,code,name,photos,rules = sorted(options,key=lambda value:value[:3])[0]
    return {"code":code,"name":name,"photos_required":photos,"unknown_rules":rules.get("unknown_rules",[]),
            "business_suitability_confirmed":False}


def prepare(source_id, agent_ids, batch, reason, photo_agents=()):
    if (not isinstance(batch,str) or not re.fullmatch(r"[a-f0-9]{32}",batch)
            or not isinstance(reason,str) or not 1 <= len(reason.strip()) <= 500):
        raise nextsls.ProtocolError("缺少本轮授权依据。")
    source = db.session.get(TmsOrder,source_id)
    if not source or source.status != "approved" or sha256(source.workbook).hexdigest() != source.file_sha256:
        raise nextsls.ProtocolError("原始审核文件不可核实。")
    if db.engine.dialect.name == "postgresql":
        db.session.execute(text("SELECT pg_advisory_xact_lock(651360351)"))
    results=[]
    for aid in agent_ids:
        if aid == source.agent_id:
            raise nextsls.ProtocolError("不能复制给已创建原单的同一供应商。")
        agent=db.session.get(CarrierAgent,aid)
        profile=db.session.get(TmsAgentApi,aid)
        if not agent or not agent.is_active or not profile or profile.provider != "nextsls":
            raise nextsls.ProtocolError("验收供应商不可用。")
        existing=db.session.scalar(select(TmsOrder).where(TmsOrder.agent_id==aid,
            TmsOrder.invoice["acceptance_source"]["batch"].as_string()==batch))
        if existing:
            chosen=existing.invoice["acceptance_source"]["candidate"]
            results.append({"agent_id":aid,"order_id":existing.id,"candidate":chosen})
            continue
        reference=f"XR-QA-{batch[:8]}-{aid}"
        raw=sample_copy(source.workbook,reference)
        invoice=deepcopy(source.invoice)
        for key in (tms_carriers.CREATE_GRANT,"supplier_tenant_origin","supplier_channel","order_archive",
                    "supplier_authorization_history","supplier_channel_reviews","supplier_event_floor",
                    "amazon_po_confirmation","amazon_po_line_confirmations"):
            invoice.pop(key,None)
        invoice["customer_number"]=reference
        invoice["metadata"]["product_note"]="API接口验收测试，不安排揽收或发货。"+invoice["metadata"].get("product_note","")
        product_photos(raw,invoice)
        chosen=candidate(invoice,agent,profile,aid in photo_agents)
        if invoice.get("nextsls_supplement"):
            invoice["nextsls_supplement"].update(agent_id=aid,service=chosen["code"] if chosen else "")
        invoice["acceptance_source"]={"batch":batch,"order_id":source.id,"file_sha256":source.file_sha256,
            "original_customer_reference":source.invoice["customer_number"],"reason":reason,"candidate":chosen,
            "fulfillment_requested":False,"creation_granted":False}
        if chosen:
            invoice.update(supplier_tenant_origin=profile.base_url,
                supplier_channel={"code":chosen["code"],"name":chosen["name"]})
        order=TmsOrder(customer_id=source.customer_id,number_key=reference,file_sha256=sha256(raw).hexdigest(),
            status="pending",invoice=invoice,workbook=raw,agent_id=aid,agent_service=chosen["code"] if chosen else None)
        db.session.add(order);db.session.flush()
        billing=db.session.get(TmsReceivable,source.id)
        if billing:
            keys=("unit","rate","surcharge","operation","high_value","currency","other_mode","other","review_note")
            draft={key:deepcopy(billing.details[key]) for key in keys if key in billing.details}
            draft["review_note"]="接口验收样本；沿用原销售服务报价草稿，须管理员重新审核。"
            db.session.add(TmsReceivable(order_id=order.id,details=draft))
        db.session.add(TmsAudit(order_id=order.id,actor_type="operator",actor_id=0,action="acceptance_copy"))
        results.append({"agent_id":aid,"order_id":order.id,"candidate":chosen})
    db.session.commit()
    return results
