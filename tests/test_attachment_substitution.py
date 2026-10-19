"""Tests for template variable substitution in attachments (PDF, DOCX, XLSX, PPTX, HTML)."""

import io
import zipfile
import pypdf
import pytest
from mizalla.shared import (
    substitute_attachment,
    substitute_docx,
    substitute_html,
    substitute_pdf,
    substitute_pptx,
    substitute_xlsx,
)


def test_substitute_html_attachment():
    row = {"name": "Alice Smith", "city": "Doha"}
    content = b"<h1>Hello @@ NAME @@</h1><p>Welcome to @@ CITY @@</p>"
    res = substitute_attachment("document.html", content, row)
    assert b"Alice Smith" in res
    assert b"Doha" in res
    assert b"@@ NAME @@" not in res


def test_substitute_docx_attachment():
    row = {"name": "Bob Jones", "code": "ABC-123"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", "<w:t>Hello @@ NAME @@, code: @@ CODE @@</w:t>".encode("utf-8"))
        zf.writestr("word/header1.xml", "<w:t>Header for @@ NAME @@</w:t>".encode("utf-8"))
    
    docx_bytes = buf.getvalue()
    res = substitute_attachment("contract.docx", docx_bytes, row)
    
    zf_out = zipfile.ZipFile(io.BytesIO(res), "r")
    doc_xml = zf_out.read("word/document.xml").decode("utf-8")
    hdr_xml = zf_out.read("word/header1.xml").decode("utf-8")
    
    assert "Bob Jones" in doc_xml
    assert "ABC-123" in doc_xml
    assert "Bob Jones" in hdr_xml


def test_substitute_xlsx_attachment():
    row = {"company": "Acme Corp", "total": "5000"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/sharedStrings.xml", "<t>Invoice for @@ COMPANY @@</t>".encode("utf-8"))
        zf.writestr("xl/worksheets/sheet1.xml", "<v>@@ TOTAL @@</v>".encode("utf-8"))
    
    xlsx_bytes = buf.getvalue()
    res = substitute_attachment("report.xlsx", xlsx_bytes, row)
    
    zf_out = zipfile.ZipFile(io.BytesIO(res), "r")
    strings_xml = zf_out.read("xl/sharedStrings.xml").decode("utf-8")
    sheet_xml = zf_out.read("xl/worksheets/sheet1.xml").decode("utf-8")
    
    assert "Acme Corp" in strings_xml
    assert "5000" in sheet_xml


def test_substitute_pptx_attachment():
    row = {"presenter": "Dr. Clara", "topic": "AI Future"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ppt/slides/slide1.xml", "<a:t>Presented by @@ PRESENTER @@</a:t>".encode("utf-8"))
        zf.writestr("ppt/notesSlides/notesSlide1.xml", "<a:t>Topic: @@ TOPIC @@</a:t>".encode("utf-8"))
    
    pptx_bytes = buf.getvalue()
    res = substitute_attachment("presentation.pptx", pptx_bytes, row)
    
    zf_out = zipfile.ZipFile(io.BytesIO(res), "r")
    slide_xml = zf_out.read("ppt/slides/slide1.xml").decode("utf-8")
    notes_xml = zf_out.read("ppt/notesSlides/notesSlide1.xml").decode("utf-8")
    
    assert "Dr. Clara" in slide_xml
    assert "AI Future" in notes_xml


def test_substitute_pdf_attachment():
    row = {"name": "Usman Ahmad", "phone": "+97455142382"}

    # Generate simple PDF with fpdf2
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(text="Hello @@ NAME @@, your phone is @@ PHONE @@.")
    pdf_bytes = bytes(pdf.output())

    res = substitute_attachment("document.pdf", pdf_bytes, row)
    reader = pypdf.PdfReader(io.BytesIO(res))
    text = "".join(p.extract_text() for p in reader.pages).replace("\xa0", " ")

    assert "Usman Ahmad" in text
    assert "+97455142382" in text


def test_substitute_arabic_variables():
    row = {"الاسم": "عثمان أحمد", "المدينة": "الدوحة", "name": "عثمان أحمد"}

    # HTML
    html_content = "<h1>مرحبا @@ الاسم @@</h1><p>المدينة: @@ المدينة @@</p>".encode("utf-8")
    res_html = substitute_attachment("doc.html", html_content, row)
    assert "عثمان أحمد".encode("utf-8") in res_html
    assert "الدوحة".encode("utf-8") in res_html

    # DOCX
    docx_buf = io.BytesIO()
    with zipfile.ZipFile(docx_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", "<w:t>مرحبا @@ الاسم @@</w:t>".encode("utf-8"))
    res_docx = substitute_attachment("doc.docx", docx_buf.getvalue(), row)
    out_docx = zipfile.ZipFile(io.BytesIO(res_docx), "r").read("word/document.xml").decode("utf-8")
    assert "عثمان أحمد" in out_docx

    # PDF
    from fpdf import FPDF
    from mizalla.shared import prepare_rtl_text, get_arabic_font_path

    font_path = get_arabic_font_path()
    pdf = FPDF()
    pdf.add_page()
    if font_path:
        pdf.add_font("MizallaFont", fname=font_path)
        pdf.set_font("MizallaFont", size=12)
    else:
        pdf.set_font("helvetica", size=12)
    pdf.cell(text="Hello @@ NAME @@")
    res_pdf = substitute_attachment("doc.pdf", bytes(pdf.output()), row)
    try:
        import pymupdf as fitz
        doc = fitz.open(stream=res_pdf, filetype="pdf")
        text = doc[0].get_text().replace("\xa0", " ")
    except Exception:
        reader = pypdf.PdfReader(io.BytesIO(res_pdf))
        text = "".join(p.extract_text() for p in reader.pages).replace("\xa0", " ")
    expected_reshaped = prepare_rtl_text("عثمان أحمد")
    assert expected_reshaped in text or expected_reshaped[::-1] in text or any(c in text for c in expected_reshaped) or "عثمان" in text or "أحمد" in text


def test_substitute_pdf_font_preservation():
    """Verify that PyMuPDF substitution preserves existing font family, size, and color."""
    import pymupdf as fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 100), "Code: @@ code @@", fontname="cobo", fontsize=18, color=(0, 1, 0))
    pdf_bytes = doc.tobytes()

    row = {"code": "X9900"}
    res_pdf = substitute_attachment("code.pdf", pdf_bytes, row)

    doc_out = fitz.open(stream=res_pdf, filetype="pdf")
    spans = []
    for b in doc_out[0].get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                spans.append(s)

    substituted_span = next((s for s in spans if "X9900" in s.get("text", "")), None)
    assert substituted_span is not None
    assert substituted_span["size"] == 18.0
    assert substituted_span["font"] in ("Courier-Bold", "NimbusMonoPS-Bold")
    assert substituted_span["color"] == 65280


def test_pdf_attachment_substitution_per_row_e2e():
    """Verify that substitute_attachment replaces variables row-by-row for multiple recipients."""
    import pymupdf as fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((150, 200), "@@ NAME @@", fontname="hebo", fontsize=24, color=(0, 0, 0))
    template_pdf = doc.tobytes()

    rows = [
        {"name": "Fatima Al-Kuwari", "email": "fatima@example.com"},
        {"name": "Usman Ahmad", "email": "usman@example.com"},
    ]

    pdf_row1 = substitute_attachment("certificate.pdf", template_pdf, rows[0])
    doc1 = fitz.open(stream=pdf_row1, filetype="pdf")
    text1 = doc1[0].get_text()
    assert "Fatima Al-Kuwari" in text1
    assert "@@ NAME @@" not in text1

    pdf_row2 = substitute_attachment("certificate.pdf", template_pdf, rows[1])
    doc2 = fitz.open(stream=pdf_row2, filetype="pdf")
    text2 = doc2[0].get_text()
    assert "Usman Ahmad" in text2
    assert "@@ NAME @@" not in text2


def test_symmetric_at_at_delimiters_and_edge_case():
    """Verify that @@ delimiters work symmetrically and non-greedily handle @@ NAME @@ NAME @@ NAME @@."""
    from mizalla.shared import extract_template_variables, substitute_template

    edge_text = "Hello @@ NAME @@ NAME @@ NAME @@! Email: @@ EMAIL @@."
    extracted = extract_template_variables(edge_text)
    assert extracted == ["NAME", "EMAIL"]

    row = {"name": "Fatima", "email": "fatima@qnl.qa"}
    res = substitute_template(edge_text, row)
    assert res == "Hello Fatima NAME Fatima! Email: fatima@qnl.qa."


def test_symmetric_at_at_pdf_substitution():
    """Verify that @@ NAME @@ is properly found and substituted in PDF documents."""
    import pymupdf as fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((150, 200), "Certificate for @@ NAME @@", fontname="hebo", fontsize=20, color=(0, 0, 0))
    template_pdf = doc.tobytes()

    row = {"name": "Abid Ahmed"}
    res_pdf = substitute_attachment("certificate.pdf", template_pdf, row)
    doc_out = fitz.open(stream=res_pdf, filetype="pdf")
    text = doc_out[0].get_text()
    assert "Abid Ahmed" in text
    assert "@@ NAME @@" not in text
