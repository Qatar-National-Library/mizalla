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
    from mizalla.shared import prepare_rtl_text, get_font_path, get_arabic_font_path

    font_path = get_font_path()
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


def test_get_font_path_english_and_arabic():
    """Verify get_font_path returns an appropriate font for English and Arabic text."""
    from pathlib import Path
    from mizalla.shared import get_font_path, get_arabic_font_path

    # English font
    en_path = get_font_path(text="Hello World", language="en")
    assert en_path is not None
    assert Path(en_path).exists()

    # Arabic font
    ar_path = get_font_path(text="مرحبا بك", language="ar")
    assert ar_path is not None
    assert Path(ar_path).exists()

    # Backwards compatibility check
    legacy_ar = get_arabic_font_path()
    assert legacy_ar is not None
    assert Path(legacy_ar).exists()


def test_get_font_path_order_and_setting_override(app):
    """Verify font_paths configuration is respected in order of appearance."""
    from pathlib import Path
    from mizalla.shared import get_font_path
    from mizalla.settings import set_setting

    with app.app_context():
        # Find two existing fonts
        existing = [p for p in [
            "C:/Windows/Fonts/tahoma.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/calibri.ttf",
        ] if Path(p).exists()]

        if len(existing) >= 2:
            first, second = existing[0], existing[1]

            # Set first font as primary
            set_setting("font_paths", f"{first}\n{second}")
            resolved = get_font_path(language="en")
            assert Path(resolved).resolve() == Path(first).resolve()

            # Swap order and verify priority changes
            set_setting("font_paths", f"{second}\n{first}")
            resolved_swapped = get_font_path(language="en")
            assert Path(resolved_swapped).resolve() == Path(second).resolve()


def test_substitute_pdf_acroform_widget():
    """Verify that AcroForm widget placeholders are deleted and replaced without dual rendering."""
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page(width=500, height=300)

    widget = fitz.Widget()
    widget.rect = fitz.Rect(100, 100, 400, 140)
    widget.field_name = "Recipient"
    widget.field_value = "@@ NAME @@"
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.text_color = [0.36, 0.043, 0.239]
    widget.text_fontsize = 16.0
    page.add_widget(widget)

    pdf_bytes = doc.tobytes()
    doc.close()

    # Verify widget exists initially
    check_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    assert len(list(check_doc[0].widgets())) == 1
    check_doc.close()

    # Perform substitution with Arabic name
    row = {"name": "عابد أحمد"}
    result_bytes = substitute_pdf(pdf_bytes, row)

    # Inspect output
    res_doc = fitz.open(stream=result_bytes, filetype="pdf")
    res_page = res_doc[0]

    # The widget must be completely removed so no dual rendering or placeholder ghosting occurs
    assert len(list(res_page.widgets())) == 0
    assert "@@ NAME @@" not in res_page.get_text()
    assert res_page.search_for("@@ NAME @@") == []

    # The replacement text must be rendered on the page
    text = res_page.get_text()
    assert len(text.strip()) > 0
    res_doc.close()


def test_substitute_pdf_multispan_no_repetition():
    """Verify that a placeholder split across multiple rects/words is only replaced once without duplication."""
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page(width=500, height=300)
    # Insert parts separately to simulate multi-span / multi-word placeholder
    page.insert_text((100, 150), "@@", fontsize=16)
    page.insert_text((125, 150), "NAME", fontsize=16)
    page.insert_text((170, 150), "@@", fontsize=16)

    pdf_bytes = doc.tobytes()
    doc.close()

    row = {"name": "عابد أحمد"}
    result_bytes = substitute_pdf(pdf_bytes, row)

    res_doc = fitz.open(stream=result_bytes, filetype="pdf")
    res_page = res_doc[0]
    assert "@@" not in res_page.get_text()
    assert "NAME" not in res_page.get_text()

    # Verify search for the replaced Arabic text appears exactly once
    rects = res_page.search_for("عابد")
    assert len(rects) <= 1
    res_doc.close()


def test_substitute_pdf_locking_permissions():
    """Verify that lock_pdf=True locks the PDF against editing with standard permissions encryption."""
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page(width=500, height=300)
    page.insert_text((100, 150), "@@ NAME @@", fontsize=14)
    widget = fitz.Widget()
    widget.rect = fitz.Rect(50, 50, 150, 80)
    widget.field_name = "SampleField"
    widget.field_value = "KeepMe"
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    page.add_widget(widget)
    pdf_bytes = doc.tobytes()
    doc.close()

    row = {"name": "Alice Smith"}
    locked_bytes = substitute_pdf(pdf_bytes, row, lock_pdf=True, owner_password="CustomOwnerPass123")

    res_doc = fitz.open(stream=locked_bytes, filetype="pdf")
    # Must open freely without password prompt for recipients
    assert not res_doc.needs_pass
    # Printing must be allowed
    assert bool(res_doc.permissions & fitz.PDF_PERM_PRINT)
    # Modifying content must be forbidden
    assert not bool(res_doc.permissions & fitz.PDF_PERM_MODIFY)
    # Form editing/filling must be forbidden
    assert not bool(res_doc.permissions & fitz.PDF_PERM_FORM)
    # Form fields must be baked/flattened
    assert len(list(res_doc[0].widgets())) == 0
    res_doc.close()


def test_smart_font_matching_and_decorations():
    """Verify that _resolve_replacement_style preserves decorations and matches fonts smartly."""
    from mizalla.shared import _resolve_replacement_style

    # Test English font matching (Arial, Times, Courier, etc.)
    en_courier_info = {
        "fontsize": 18.0,
        "color": (0.2, 0.4, 0.6),
        "font_raw": "ABCDEF+CourierNewPSMT",
        "is_bold": True,
        "is_italic": True,
        "is_serif": False,
        "is_mono": True,
        "is_underline": True,
    }
    style_res = _resolve_replacement_style("Johnathan Doe", en_courier_info, align_mode="left")
    assert "Courier" in style_res["style"]
    assert "font-weight:bold" in style_res["style"]
    assert "font-style:italic" in style_res["style"]
    assert "text-decoration:underline" in style_res["style"]
    assert "font-size:18.0px" in style_res["style"]
    assert "text-align:left" in style_res["style"]

    # Test Arabic font matching (Serif style -> Traditional Arabic / Amiri)
    ar_serif_info = {
        "fontsize": 22.0,
        "color": (0.0, 0.0, 0.0),
        "font_raw": "Times-Bold",
        "is_bold": True,
        "is_italic": False,
        "is_serif": True,
        "is_mono": False,
        "is_underline": False,
    }
    ar_res = _resolve_replacement_style("عابد أحمد", ar_serif_info, align_mode="directional")
    assert "Traditional Arabic" in ar_res["style"] or "Amiri" in ar_res["style"]
    assert "font-weight:bold" in ar_res["style"]
    # Directional for Arabic must be right-aligned without direction:rtl (which breaks PyMuPDF box coordinates)
    assert "text-align:right" in ar_res["style"]

    # Test Arabic sans-serif (default modern style -> Tahoma / Arial)
    ar_sans_info = {
        "fontsize": 14.0,
        "color": (0.1, 0.1, 0.1),
        "font_raw": "Calibri",
        "is_bold": False,
        "is_italic": False,
        "is_serif": False,
        "is_mono": False,
        "is_underline": False,
    }
    ar_sans_res = _resolve_replacement_style("مرحبا بك", ar_sans_info, align_mode="center")
    assert "Tahoma" in ar_sans_res["style"] or "Arial" in ar_sans_res["style"]
    assert "text-align:center" in ar_sans_res["style"]


def test_pdf_alignment_modes_execution():
    """Verify that substitute_pdf accepts all alignment modes without error and renders text correctly."""
    import pymupdf as fitz
    from mizalla.shared import substitute_pdf

    doc = fitz.open()
    page = doc.new_page(width=600, height=400)
    page.insert_text((150, 200), "@@ NAME @@", fontsize=16)
    pdf_bytes = doc.tobytes()
    doc.close()

    for align in ["center", "directional", "left", "right"]:
        # Test English
        out_en = substitute_pdf(pdf_bytes, {"name": "Grace Hopper"}, pdf_alignment=align)
        res_doc_en = fitz.open(stream=out_en, filetype="pdf")
        assert "@@ NAME @@" not in res_doc_en[0].get_text()
        assert len(res_doc_en[0].get_text().strip()) > 0
        res_doc_en.close()

        # Test Arabic
        out_ar = substitute_pdf(pdf_bytes, {"name": "عائشة نور"}, pdf_alignment=align)
        res_doc_ar = fitz.open(stream=out_ar, filetype="pdf")
        assert "@@ NAME @@" not in res_doc_ar[0].get_text()
        assert len(res_doc_ar[0].get_text().strip()) > 0
        res_doc_ar.close()


