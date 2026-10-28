"""Test fixtures: tiny valid documents generated in code (no personal data)."""

import io

from pypdf import PdfWriter


def text_pdf(pages: list[str]) -> bytes:
    """Minimal valid text PDF, one string per page."""
    objs: list[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    n = len(pages)
    font = 3 + 2 * n
    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(
        (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
            + f"] /Count {n} >>"
        ).encode()
    )
    for i, t in enumerate(pages):
        add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({t}) Tj ET".encode()
        add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def image_only_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(612, 792)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


MD_DOC = (
    b"# Guide\nIntro paragraph about quasar telemetry.\n\n## Install\nRun the installer twice.\n"
)
CSV_DOC = b"name,city,note\nAda,London,pioneer of computing\nLinus,Helsinki,kernel author\n"
JSON_DOC = (
    b'[{"sku":"A1","desc":"red widget","dims":{"w":2,"h":3}},{"sku":"B2","desc":"blue gadget"}]'
)
JSONL_DOC = b'{"id":1,"msg":"alpha event"}\n{"id":2,"msg":"beta event"}\n'
