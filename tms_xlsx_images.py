"""Read internal Excel/WPS image links without executing worksheet formulas."""
from io import BytesIO
import posixpath
import re
from xml.etree.ElementTree import fromstring

from PIL import Image


XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
ART_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
FIRST_PRODUCT_ROW = 23
MAX_PRODUCT_LINES = 60
LAST_PRODUCT_ROW = FIRST_PRODUCT_ROW + MAX_PRODUCT_LINES - 1
PHOTO_CELL = re.compile(r"O(?:2[3-9]|[3-7][0-9]|8[0-2])\Z")
MEDIA_PART = re.compile(r"xl/media/[A-Za-z0-9_.-]+\.(?:png|jpe?g)\Z", re.IGNORECASE)
WPS_IMAGE_FORMULA = re.compile(r'(?:_xlfn\.)?DISPIMG\(\s*"(ID_[A-Za-z0-9_-]{1,64})"\s*,\s*[12]\s*\)\Z',
                               re.IGNORECASE)
MAX_PHOTO_BYTES = 2 * 1024 * 1024
MAX_PHOTO_TOTAL_BYTES = 8 * 1024 * 1024
MAX_PHOTO_PIXELS = 12_000_000


class PhotoError(ValueError):
    pass


def relations(archive, part, kind):
    folder, name = posixpath.split(part)
    rels = posixpath.join(folder, "_rels", name + ".rels")
    if rels not in archive.namelist():
        return {}
    result = {}
    seen = set()
    for entry in fromstring(archive.read(rels)):
        key = entry.attrib["Id"]
        if key in seen:
            raise PhotoError("工作簿包含重复的内部关系，无法唯一关联图片。")
        seen.add(key)
        if entry.get("TargetMode", "").casefold() == "external" or not entry.get("Type", "").endswith("/" + kind):
            continue
        target = entry.attrib["Target"]
        result[key] = posixpath.normpath(
            target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target))
    return result


def invoice_sheet_part(archive):
    root = fromstring(archive.read("xl/workbook.xml"))
    links = relations(archive, "xl/workbook.xml", "worksheet")
    for sheet in root.iter(XLSX_NS + "sheet"):
        if sheet.get("name") == "星睿发票":
            return links.get(sheet.get(REL_NS + "id"))
    return None


def image_type(part, data):
    if not MEDIA_PART.fullmatch(part):
        return None
    if part.lower().endswith(".png") and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if part.lower().endswith((".jpg", ".jpeg")) and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def read_photo(archive, part):
    """Return the original bytes after verifying both the container and decoded image."""
    if not part or not MEDIA_PART.fullmatch(part):
        raise PhotoError("产品图片必须是工作簿内嵌的 PNG 或 JPEG。")
    try:
        entry = archive.getinfo(part)
    except KeyError:
        raise PhotoError("找不到内嵌的产品图片。") from None
    if entry.file_size > MAX_PHOTO_BYTES:
        raise PhotoError("单张产品图片不可超过 2 MiB。")
    data = archive.read(entry)
    mime = image_type(part, data)
    if not mime:
        raise PhotoError("产品图片的扩展名与内容不符。")
    image_format = "PNG" if mime == "image/png" else "JPEG"
    try:
        with Image.open(BytesIO(data), formats=(image_format,)) as image:
            if image.width * image.height > MAX_PHOTO_PIXELS or getattr(image, "n_frames", 1) != 1:
                raise PhotoError("产品图片必须为静态图片，且不可超过 1200 万像素。")
            image.verify()
        with Image.open(BytesIO(data), formats=(image_format,)) as image:
            image.load()
    except PhotoError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise PhotoError("产品图片已损坏或无法解码。") from None
    return mime, data


def validate_photos(archive, photos):
    sizes = {}
    for cell, part in photos.items():
        if part not in sizes:
            try:
                sizes[part] = len(read_photo(archive, part)[1])
            except PhotoError as exc:
                raise PhotoError(f"{cell}（产品图片）：{exc}") from None
    if sum(sizes[part] for part in photos.values()) > MAX_PHOTO_TOTAL_BYTES:
        raise PhotoError("产品申报图片合计不可超过 8 MiB。")


def wps_images(archive):
    part = "xl/cellimages.xml"
    if part not in archive.namelist():
        return {}
    media = relations(archive, part, "image")
    photos = {}
    for picture in fromstring(archive.read(part)).iter(DRAWING_NS + "pic"):
        properties = picture.find(".//" + DRAWING_NS + "cNvPr")
        image = picture.find(".//" + ART_NS + "blip")
        if properties is None or image is None:
            continue
        name = properties.get("name")
        target = media.get(image.get(REL_NS + "embed"))
        if name:
            photos[name] = None if name in photos else target
    return photos


def anchored_images(archive, sheet_part):
    if not sheet_part:
        return {}
    photos = {}
    drawings = relations(archive, sheet_part, "drawing")
    sheet = fromstring(archive.read(sheet_part))
    for link in sheet.findall(XLSX_NS + "drawing"):
        drawing = drawings.get(link.get(REL_NS + "id"), "")
        if not re.fullmatch(r"xl/drawings/drawing\d+\.xml", drawing):
            continue
        media = relations(archive, drawing, "image")
        for anchor in fromstring(archive.read(drawing)):
            if anchor.tag not in (DRAWING_NS + "oneCellAnchor", DRAWING_NS + "twoCellAnchor"):
                continue
            start = anchor.find(DRAWING_NS + "from")
            pictures = anchor.findall(DRAWING_NS + "pic")
            if start is None or not pictures or start.findtext(DRAWING_NS + "col") != "14":
                continue
            row = start.findtext(DRAWING_NS + "row", "")
            if not re.fullmatch(r"\d{1,7}", row):
                continue
            line = int(row) + 1
            if line > LAST_PRODUCT_ROW:
                raise PhotoError(f"O{line}（产品图片）超出申报区。")
            cell = f"O{line}"
            if PHOTO_CELL.fullmatch(cell):
                if cell in photos or len(pictures) != 1:
                    raise PhotoError(f"{cell}（产品图片）有多张浮动图片，请保留一张。")
                images = pictures[0].findall(DRAWING_NS + "blipFill/" + ART_NS + "blip")
                if len(images) != 1:
                    raise PhotoError(f"{cell}（产品图片）无法读取唯一的内嵌图片。")
                photos[cell] = media.get(images[0].get(REL_NS + "embed"))
    return photos
