"""Private, order-scoped NextSLS image delivery. No carrier credentials go to storage."""
from contextlib import closing
from dataclasses import dataclass, field
from hashlib import sha256
from http.client import HTTPSConnection
import re
from urllib.parse import parse_qs, urlsplit

from flask import current_app

import nextsls
from tms_xlsx_images import MAX_PHOTO_BYTES, MAX_PHOTO_TOTAL_BYTES


# Use (tenant, None) for a verified tenant-wide contract or (tenant, channel) for a verified exception.
CONFIRMED_PHOTOS_ENCODERS = {}
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg"}
MEDIA_ERROR = "商品图片未能通过私有存储的 HTTPS 读取校验，未提交代理下单。"


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
    config = current_app.config
    required = nextsls.IMAGE_FIELDS.intersection(rules["require_declaration_list"])
    account_scope = f"{order.agent_id}|{profile.base_url}"
    legacy_scope = f"{account_scope}|{order.agent_service}"
    if (not required or config.get("ISOLATED_MODE") or profile.provider != "nextsls"
            or profile.agent_id != order.agent_id or config.get("TMS_NEXTSLS_IMAGE_SCOPE") not in (account_scope, legacy_scope)):
        return None, {}
    encoders = {"photo_url": str} if "photo_url" in required else {}
    encoder = CONFIRMED_PHOTOS_ENCODERS.get(
        (profile.base_url, order.agent_service), CONFIRMED_PHOTOS_ENCODERS.get((profile.base_url, None)),
    )
    if "photos" in required and encoder is not None:
        encoders["photos"] = encoder
    bucket = config.get("TMS_NEXTSLS_IMAGE_BUCKET", "")
    region = config.get("TMS_NEXTSLS_IMAGE_REGION", "")
    ttl = config.get("TMS_NEXTSLS_IMAGE_URL_TTL_SECONDS", "")
    key = config.get("TMS_NEXTSLS_IMAGE_ACCESS_KEY_ID", "")
    secret = config.get("TMS_NEXTSLS_IMAGE_SECRET_ACCESS_KEY", "")
    if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)
            or not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d", region)
            or not re.fullmatch(r"[0-9]{3,6}", ttl) or not 300 <= int(ttl) <= 604800
            or not key or not secret):
        return None, {}
    return ImageStore(bucket, region, int(ttl), key, secret,
                      config.get("TMS_NEXTSLS_IMAGE_SESSION_TOKEN", "")), encoders


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


def image_get(host, target, *, read_content):
    connection = HTTPSConnection(host, timeout=5)
    try:
        connection.request("GET", target)
        response = connection.getresponse()
        return (response.status, response.getheader("Content-Type"),
                response.read(MAX_PHOTO_BYTES + 1) if read_content and response.status == 200 else None)
    finally:
        connection.close()


def publish_images(store, order, profile, photos, encoders):
    """Publish only the images in this order, then fetch each signed URL over HTTPS."""
    if current_app.config.get("ISOLATED_MODE"):
        raise nextsls.ProtocolError("隔离模式禁止真实图片上传。")
    if (profile.agent_id != order.agent_id or not photos or len(photos) != len(order.invoice["declarations"])
            or not encoders or not encoders.keys() <= nextsls.IMAGE_FIELDS
            or any(mime not in IMAGE_TYPES or type(data) is not bytes or not 0 < len(data) <= MAX_PHOTO_BYTES
                   for mime, data in photos) or sum(len(data) for _, data in photos) > MAX_PHOTO_TOTAL_BYTES):
        raise nextsls.ProtocolError(MEDIA_ERROR)
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
            for index, (mime, data) in enumerate(photos):
                digest = sha256(data).hexdigest()
                key = f"nextsls/{prefix}/{index}-{digest}.{IMAGE_TYPES[mime]}"
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
                if image_get(store.host, f"{target.path}?{target.query}", read_content=True) != (200, mime, data):
                    raise ValueError("unexpected image content")
                references.append({name: encoder(url) for name, encoder in encoders.items()})
    except Exception:
        # Transport errors can contain a complete signed URL. Never propagate them to the web or audit log.
        raise nextsls.ProtocolError(MEDIA_ERROR) from None
    return references
