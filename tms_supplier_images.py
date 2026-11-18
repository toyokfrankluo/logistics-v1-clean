"""Private, order-scoped NextSLS image delivery. No carrier credentials go to storage."""
from contextlib import closing
from dataclasses import dataclass, field
from hashlib import sha256
from http.client import HTTPSConnection
import re
from urllib.parse import parse_qs, urlsplit

from flask import current_app

import nextsls
import tms_supplier_files as direct
from tms_xlsx_images import MAX_PHOTO_BYTES, MAX_PHOTO_TOTAL_BYTES


# Shared V5 default selected by the operator. This is not evidence of vendor acceptance.
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg"}
MEDIA_ERROR = "未能生成可读取的 HTTPS 商品图片链接，未提交代理下单。"


@dataclass(frozen=True)
class ImageStore:
    bucket: str
    region: str
    ttl: int
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    session_token: str = field(repr=False)

    @property
    def host(self):
        return f"{self.bucket}.s3.{self.region}.amazonaws.com"


def image_delivery(order, profile, rules):
    """Resolve only the configured account/channel and the fields it actually requires."""
    encoders = image_field_encoders(order.invoice, profile, order.agent_service, rules)
    store = media_store(order, profile) if encoders else None
    return (store, encoders) if store else (None, {})


def image_field_encoders(invoice, profile, channel, rules):
    required = nextsls.IMAGE_FIELDS.intersection(nextsls.declaration_requirements(rules))
    if rules.get("require_declaration_list") is None and invoice["declarations"] and all(
            item.get("image_part") for item in invoice["declarations"]):
        required = {"photos"}
    encoders = {"photo_url": str} if "photo_url" in required else {}
    encoder = photos_encoder(profile, channel)
    if "photos" in required and encoder is not None:
        encoders["photos"] = encoder
    return encoders


def photos_encoder(profile, channel):
    if not profile or profile.provider != "nextsls":
        return None
    # Explicit, evidence-backed exceptions are configuration, never supplier-name branches.
    verified = getattr(profile, "verified", None) or {}
    override = verified.get("photos_protocols", {}).get(channel, verified.get("photos_protocol", {}))
    if override.get("encoding") == "unsupported" and override.get("evidence"):
        return None
    return nextsls.product_photo_url


def media_store(order, profile):
    # The site's own signed file URLs are the default. Existing S3 settings
    # can still be used by installations without a direct HTTPS origin.
    return direct.direct_store(order, profile) or s3_media_store(order, profile)


def s3_media_store(order, profile):
    config = current_app.config
    account_scope = f"{order.agent_id}|{profile.base_url}"
    legacy_scope = f"{account_scope}|{order.agent_service}"
    if (config.get("ISOLATED_MODE") or profile.provider != "nextsls"
            or profile.agent_id != order.agent_id
            or config.get("TMS_NEXTSLS_IMAGE_SCOPE") not in (account_scope, legacy_scope)):
        return None
    bucket = config.get("TMS_NEXTSLS_IMAGE_BUCKET", "")
    region = config.get("TMS_NEXTSLS_IMAGE_REGION", "")
    ttl = config.get("TMS_NEXTSLS_IMAGE_URL_TTL_SECONDS", "")
    key = config.get("TMS_NEXTSLS_IMAGE_ACCESS_KEY_ID", "")
    secret = config.get("TMS_NEXTSLS_IMAGE_SECRET_ACCESS_KEY", "")
    if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)
            or not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d", region)
            or not re.fullmatch(r"[0-9]{3,6}", ttl) or not 300 <= int(ttl) <= 604800
            or not key or not secret):
        return None
    return ImageStore(bucket, region, int(ttl), key, secret,
                      config.get("TMS_NEXTSLS_IMAGE_SESSION_TOKEN", ""))


def s3_client(store):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3", region_name=store.region, endpoint_url=f"https://s3.{store.region}.amazonaws.com",
        aws_access_key_id=store.access_key, aws_secret_access_key=store.secret_key,
        aws_session_token=store.session_token or None,
        config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=5,
                      proxies={}, retries={"total_max_attempts": 1}, s3={"addressing_style": "virtual"}),
    )


def image_get(host, target, *, read_content, limit=MAX_PHOTO_BYTES):
    connection = HTTPSConnection(host, timeout=5)
    try:
        connection.request("GET", target)
        response = connection.getresponse()
        return (response.status, response.getheader("Content-Type"),
                response.read(limit + 1) if read_content and response.status == 200 else None)
    finally:
        connection.close()


def publish_images(store, order, profile, photos, encoders, *, preview=False):
    """Publish only the original image associated with each declaration."""
    if (profile.agent_id != order.agent_id or not photos or len(photos) != len(order.invoice["declarations"])
            or not encoders or not encoders.keys() <= nextsls.IMAGE_FIELDS
            or any(mime not in IMAGE_TYPES or type(data) is not bytes or not 0 < len(data) <= MAX_PHOTO_BYTES
                   for mime, data in photos) or sum(len(data) for _, data in photos) > MAX_PHOTO_TOTAL_BYTES):
        raise nextsls.ProtocolError(MEDIA_ERROR)
    files = [(str(index), IMAGE_TYPES[mime], mime, data) for index, (mime, data) in enumerate(photos)]
    urls = publish_files(store, order, profile, files, MEDIA_ERROR, preview=preview)
    return encode_image_urls(urls, encoders)


def encode_image_urls(urls, encoders):
    try:
        return [{name: encoder(url) for name, encoder in encoders.items()} for url in urls]
    except Exception:
        raise nextsls.ProtocolError(MEDIA_ERROR) from None


PACKING_MIME = direct.PACKING_MIME
PACKING_ERROR = "未能生成可读取的 HTTPS 装箱单链接，未提交代理下单。"


def publish_packing(store, order, profile, data, *, preview=False):
    if not data or len(data) > MAX_PHOTO_TOTAL_BYTES:
        raise nextsls.ProtocolError(PACKING_ERROR)
    return publish_files(store, order, profile, [("packing", "xlsx", PACKING_MIME, data)], PACKING_ERROR,
                         preview=preview)[0]


def publish_files(store, order, profile, files, error, *, preview=False):
    if profile.agent_id != order.agent_id:
        raise nextsls.ProtocolError(error)
    if isinstance(store, direct.DirectStore):
        return direct.stage_files(store, order, files, preview=preview)
    if current_app.config.get("ISOLATED_MODE") or preview:
        raise nextsls.ProtocolError(error)
    scope = f"{profile.base_url}|{order.agent_id}|{order.agent_service}|{order.id}|{order.revision}"
    prefix = sha256(scope.encode()).hexdigest()
    references = []
    try:
        with closing(s3_client(store)) as client:
            protection = client.get_public_access_block(Bucket=store.bucket)["PublicAccessBlockConfiguration"]
            if not all(protection.get(key) is True for key in (
                "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets",
            )):
                raise ValueError("bucket public access is not blocked")
            for name, extension, mime, data in files:
                digest = sha256(data).hexdigest()
                key = f"nextsls/{prefix}/{name}-{digest}.{extension}"
                client.put_object(Bucket=store.bucket, Key=key, Body=data, ContentType=mime,
                                  CacheControl="private, no-store", ServerSideEncryption="AES256")
                if image_get(store.host, f"/{key}", read_content=False)[0] not in (403, 404):
                    raise ValueError("unsigned access not blocked")
                url = client.generate_presigned_url("get_object", Params={"Bucket": store.bucket, "Key": key},
                                                    ExpiresIn=store.ttl, HttpMethod="GET")
                target = urlsplit(nextsls.product_photo_url(url))
                query = parse_qs(target.query)
                if (target.netloc != store.host or target.path != f"/{key}"
                        or query.get("X-Amz-Algorithm") != ["AWS4-HMAC-SHA256"]
                        or query.get("X-Amz-Expires") != [str(store.ttl)]
                        or not query.get("X-Amz-Signature")):
                    raise ValueError("unexpected signed URL")
                read_options = {"limit": len(data)} if extension == "xlsx" else {}
                if image_get(store.host, f"{target.path}?{target.query}", read_content=True, **read_options) != (200, mime, data):
                    raise ValueError("unexpected image content")
                references.append(url)
    except Exception:
        # Transport errors can contain a complete signed URL. Never propagate them to the web or audit log.
        raise nextsls.ProtocolError(error) from None
    return references
