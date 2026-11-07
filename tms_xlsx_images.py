"""Read internal Excel/WPS image links without executing worksheet formulas."""
import posixpath
import re
from xml.etree.ElementTree import fromstring


XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
ART_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
PHOTO_CELL = re.compile(r"O(?:2[3-9]|[34][0-9]|5[0-2])\Z")
MEDIA_PART = re.compile(r"xl/media/[A-Za-z0-9_.-]+\.(?:png|jpe?g)\Z", re.IGNORECASE)
WPS_IMAGE_FORMULA = re.compile(r'(?:_xlfn\.)?DISPIMG\(\s*"(ID_[A-Za-z0-9_-]{1,64})"\s*,\s*[12]\s*\)\Z',
                               re.IGNORECASE)


def relations(archive, part, kind):
    folder, name = posixpath.split(part)
    rels = posixpath.join(folder, "_rels", name + ".rels")
    if rels not in archive.namelist():
        return {}
    result = {}
    for entry in fromstring(archive.read(rels)):
        if entry.get("TargetMode", "").casefold() == "external" or not entry.get("Type", "").endswith("/" + kind):
            continue
        target = entry.attrib["Target"]
        result[entry.attrib["Id"]] = posixpath.normpath(
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


def valid_media(archive, part):
    if not part or not MEDIA_PART.fullmatch(part) or part not in archive.namelist():
        return False
    with archive.open(part) as file:
        return bool(image_type(part, file.read(8)))


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
        if name and valid_media(archive, target):
            photos[name] = target
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
            start = anchor.find(DRAWING_NS + "from")
            image = anchor.find(DRAWING_NS + "pic/" + DRAWING_NS + "blipFill/" + ART_NS + "blip")
            if start is None or image is None or start.findtext(DRAWING_NS + "col") != "14":
                continue
            row = start.findtext(DRAWING_NS + "row", "")
            if not re.fullmatch(r"\d{1,2}", row):
                continue
            cell = f"O{int(row) + 1}"
            part = media.get(image.get(REL_NS + "embed"))
            if PHOTO_CELL.fullmatch(cell) and valid_media(archive, part):
                photos.setdefault(cell, part)
    return photos
