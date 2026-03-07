import io
import zipfile

from PIL import Image

from synapse.attachments import attachment_prompt_context, enrich_attachment


def test_enrich_attachment_parses_plain_text_document() -> None:
    enriched = enrich_attachment(
        {
            "kind": "document",
            "file_name": "notes.txt",
            "mime_type": "text/plain",
        },
        b"hello from file\nsecond line",
    )

    assert enriched["content_status"] == "parsed"
    assert "hello from file" in enriched["content_preview"]


def test_enrich_attachment_parses_xlsx_document() -> None:
    payload = build_minimal_xlsx()

    enriched = enrich_attachment(
        {
            "kind": "document",
            "file_name": "sheet.xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
        payload,
    )

    assert enriched["content_status"] == "parsed"
    assert "hello" in enriched["content_preview"]


def test_attachment_prompt_context_prefers_content_preview() -> None:
    context = attachment_prompt_context(
        [
            {
                "kind": "document",
                "file_name": "report.txt",
                "content_preview": "hello world",
            }
        ]
    )

    assert "document (report.txt): hello world" in context


def test_enrich_attachment_inlines_small_image() -> None:
    buffer = io.BytesIO()
    image = Image.new("RGB", (4, 3), color="red")
    image.save(buffer, format="PNG")

    enriched = enrich_attachment(
        {
            "kind": "photo",
            "file_name": "tiny.png",
            "mime_type": "image/png",
        },
        buffer.getvalue(),
    )

    assert enriched["content_status"] == "parsed"
    assert enriched["image_width"] == 4
    assert enriched["image_height"] == 3
    assert str(enriched["inline_data_url"]).startswith("data:image/png;base64,")


def test_enrich_attachment_parses_pdf_document() -> None:
    pdf = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 44 >>
stream
BT
/F1 24 Tf
72 100 Td
(Hello PDF) Tj
ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000010 00000 n 
0000000063 00000 n 
0000000122 00000 n 
0000000248 00000 n 
0000000342 00000 n 
trailer
<< /Root 1 0 R /Size 6 >>
startxref
412
%%EOF
"""

    enriched = enrich_attachment(
        {
            "kind": "document",
            "file_name": "hello.pdf",
            "mime_type": "application/pdf",
        },
        pdf,
    )

    assert enriched["content_status"] == "parsed"
    assert "Hello PDF" in enriched["content_preview"]


def build_minimal_xlsx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
            <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1">
              <si><t>hello</t></si>
            </sst>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="1"><c r="A1" t="s"><v>0</v></c></row>
              </sheetData>
            </worksheet>""",
        )
    return buffer.getvalue()
