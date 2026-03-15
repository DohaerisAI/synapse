from __future__ import annotations

import csv
import base64
import io
import zipfile
from xml.etree import ElementTree

try:
    from PIL import Image
except ImportError:  # pragma: no cover - depends on optional Pillow install
    Image = None  # type: ignore[assignment]


MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
PREVIEW_CHARS = 1500
INLINE_IMAGE_BYTES = 3 * 1024 * 1024


def enrich_attachment(attachment: dict[str, object], data: bytes | None) -> dict[str, object]:
    enriched = dict(attachment)
    if not data:
        return enriched
    enriched["content_bytes"] = len(data)
    if len(data) > MAX_ATTACHMENT_BYTES:
        enriched["content_status"] = "too_large"
        return enriched
    file_name = str(enriched.get("file_name", "") or "")
    mime_type = str(enriched.get("mime_type", "") or "")
    kind = str(enriched.get("kind", "") or "")
    image_result = _extract_image_payload(data, file_name=file_name, mime_type=mime_type, kind=kind)
    if image_result is not None:
        enriched.update(image_result)
        return enriched
    extracted = _extract_text(data, file_name=file_name, mime_type=mime_type)
    if not extracted:
        enriched["content_status"] = "unsupported"
        return enriched
    enriched["content_status"] = "parsed"
    enriched["content_preview"] = extracted[:PREVIEW_CHARS]
    return enriched


def attachment_prompt_context(attachments: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for attachment in attachments:
        kind = str(attachment.get("kind", "attachment"))
        file_name = str(attachment.get("file_name", "")).strip()
        header = kind if not file_name else f"{kind} ({file_name})"
        preview = str(attachment.get("content_preview", "")).strip()
        if preview:
            parts.append(f"{header}: {preview}")
        else:
            mime_type = str(attachment.get("mime_type", "")).strip()
            parts.append(header if not mime_type else f"{header} [{mime_type}]")
    return "\n\n".join(parts)


def _extract_text(data: bytes, *, file_name: str, mime_type: str) -> str:
    lowered_name = file_name.lower()
    lowered_mime = mime_type.lower()
    if lowered_name.endswith(".pdf") or "pdf" in lowered_mime:
        return _extract_pdf(data)
    if lowered_name.endswith(".xlsx") or "spreadsheetml" in lowered_mime:
        return _extract_xlsx(data)
    if lowered_name.endswith(".docx") or "wordprocessingml" in lowered_mime:
        return _extract_docx(data)
    if lowered_name.endswith(".csv") or lowered_mime == "text/csv":
        return _extract_csv(data)
    if lowered_name.endswith(".txt") or lowered_mime.startswith("text/"):
        return _extract_text_file(data)
    return ""


def _extract_image_payload(
    data: bytes,
    *,
    file_name: str,
    mime_type: str,
    kind: str,
) -> dict[str, object] | None:
    lowered_name = file_name.lower()
    lowered_mime = mime_type.lower()
    if kind not in {"image", "photo"} and not lowered_mime.startswith("image/") and not lowered_name.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif")
    ):
        return None
    if Image is None:
        return {"content_status": "unsupported"}
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        return {"content_status": "unsupported"}
    detected_mime = mime_type or Image.MIME.get(image.format, "") or "image/png"
    preview = f"image {image.width}x{image.height}"
    if image.format:
        preview = f"{preview} {image.format}"
    payload: dict[str, object] = {
        "mime_type": detected_mime,
        "content_status": "parsed",
        "content_preview": preview,
        "image_width": image.width,
        "image_height": image.height,
    }
    if len(data) <= INLINE_IMAGE_BYTES:
        payload["inline_data_url"] = "data:" + detected_mime + ";base64," + base64.b64encode(data).decode("ascii")
    else:
        payload["content_status"] = "too_large"
    return payload


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    reader = PdfReader(io.BytesIO(data))
    texts = []
    for page in reader.pages[:10]:
        extracted = page.extract_text() or ""
        if extracted.strip():
            texts.append(extracted.strip())
    return "\n\n".join(texts).strip()


def _extract_xlsx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        shared_strings = _xlsx_shared_strings(archive)
        rows: list[str] = []
        for name in sorted(item for item in archive.namelist() if item.startswith("xl/worksheets/sheet") and item.endswith(".xml")):
            xml_root = ElementTree.fromstring(archive.read(name))
            namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for row in xml_root.findall(".//x:sheetData/x:row", namespace):
                values: list[str] = []
                for cell in row.findall("x:c", namespace):
                    cell_type = cell.get("t", "")
                    value = cell.find("x:v", namespace)
                    if value is None or value.text is None:
                        continue
                    cell_text = value.text
                    if cell_type == "s":
                        try:
                            idx = int(cell_text)
                            cell_text = shared_strings[idx]
                        except (ValueError, IndexError):
                            pass
                    values.append(cell_text)
                if values:
                    rows.append("\t".join(values))
        return "\n".join(rows).strip()


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values: list[str] = []
    for item in root.findall("x:si", namespace):
        texts = [node.text or "" for node in item.findall(".//x:t", namespace)]
        values.append("".join(texts))
    return values


def _extract_docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if "word/document.xml" not in archive.namelist():
            return ""
        root = ElementTree.fromstring(archive.read("word/document.xml"))
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = []
        for paragraph in root.findall(".//w:p", namespace):
            text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace)).strip()
            if text:
                paragraphs.append(text)
        return "\n".join(paragraphs).strip()


def _extract_csv(data: bytes) -> str:
    text = data.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    rows = ["\t".join(row) for row in list(reader)[:100] if row]
    return "\n".join(rows).strip()


def _extract_text_file(data: bytes) -> str:
    return data.decode("utf-8", errors="ignore").strip()
