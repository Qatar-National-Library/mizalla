"""Shared utilities for template substitution and attachment processing."""

import base64
import functools
import io
import re
import zipfile

# Strictly support symmetric @@ var @@ delimiters (non-greedy, immune to BiDi/RTL reversals)
TEMPLATE_PATTERN = re.compile(
    r"@@\s*([^@]+?)\s*@@",
    re.IGNORECASE | re.DOTALL,
)


def extract_template_variables(text: str) -> list[str]:
    """Extract unique variable names from text matching @@ VAR @@."""
    if not text:
        return []
    vars_list = []
    for match in TEMPLATE_PATTERN.finditer(text):
        raw_name = match.group(1)
        if raw_name:
            clean_name = raw_name.replace("\xa0", " ").strip().strip(BIDI_BOM_CHARS).strip()
            if clean_name and clean_name not in vars_list:
                vars_list.append(clean_name)
    return vars_list


BIDI_BOM_CHARS = "\ufeff\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"


def is_rtl(text: str) -> bool:
    """Check if text contains right-to-left (Arabic/Hebrew) characters."""
    if not text:
        return False
    return any(
        '\u0600' <= char <= '\u06FF' or  # Arabic
        '\u0750' <= char <= '\u077F' or  # Arabic Supplement
        '\u08A0' <= char <= '\u08FF' or  # Arabic Extended-A
        '\uFB50' <= char <= '\uFDFF' or  # Arabic Presentation Forms-A
        '\uFE70' <= char <= '\uFEFF' or  # Arabic Presentation Forms-B
        '\u0590' <= char <= '\u05FF'     # Hebrew
        for char in text
    )


def prepare_rtl_text(text: str) -> str:
    """Reshape Arabic glyphs and apply BiDi visual character ordering.

    ReportLab does not support Arabic/RTL text out of the box. Using
    arabic-reshaper and python-bidi connects letter glyphs and orders
    characters visually for ReportLab and PDF rendering.
    """
    if not text or not is_rtl(text):
        return text

    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception:
        return text


DEFAULT_FONT_PATHS = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    "C:/Windows/Fonts/seguiemj.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
    "/Library/Fonts/Helvetica.ttf",
]


@functools.lru_cache(maxsize=128)
def _check_font_language_support(font_path: str, is_arabic: bool) -> bool:
    """Inspect TTF/OTF font file cmap table to verify support for Arabic or English."""
    try:
        from fontTools.ttLib import TTFont
        with TTFont(font_path, fontNumber=0) as font:
            cmap = font.getBestCmap()
            if not cmap:
                return True
            if is_arabic:
                # Arabic: check for 0x0627 (Alef) or common Arabic range characters
                return any(cp in cmap for cp in (0x0627, 0x0628, 0x0645, 0x0644))
            else:
                # English: check for 0x0041 ('A') or basic Latin letters
                return 0x0041 in cmap or 0x0061 in cmap
    except Exception:
        # Fallback heuristic based on font name/path
        p = font_path.lower()
        if is_arabic:
            return any(name in p for name in ["arial", "tahoma", "segoe", "segui", "notosansarabic", "freesans", "dejavusans"])
        return True


def get_font_paths_list() -> list[str]:
    """Retrieve the configured font paths list from settings, falling back to defaults."""
    try:
        from mizalla.settings import get_setting
        raw = get_setting("font_paths")
        if raw:
            paths = [line.strip() for line in raw.splitlines() if line.strip()]
            if paths:
                return paths
    except Exception:
        pass
    return list(DEFAULT_FONT_PATHS)


def get_font_path(text: str = None, language: str = None) -> str | None:
    """Return the file path to the first TrueType/OpenType font supporting the target language.

    Parameters:
        text: Optional sample text to detect language from via RTL check.
        language: Optional language code ('ar'/'arabic' or 'en'/'english'). If not provided,
                  inferred from text; if neither provided, defaults to Arabic support.
    """
    from pathlib import Path

    if language:
        is_arabic = language.lower() in ("ar", "arabic")
    elif text:
        is_arabic = is_rtl(text)
    else:
        is_arabic = True

    configured_paths = get_font_paths_list()

    # Pass 1: Look for fonts in the order they appear and load the first appropriate one
    for path_str in configured_paths:
        p = Path(path_str)
        if p.is_file() and _check_font_language_support(str(p), is_arabic=is_arabic):
            return str(p)

    # Pass 2: Fallback to the first existing font file regardless of language check
    for path_str in configured_paths:
        p = Path(path_str)
        if p.is_file():
            return str(p)

    return None


def get_arabic_font_path() -> str | None:
    """Backwards-compatible alias for get_font_path(language='ar')."""
    return get_font_path(language="ar")


def _normalize_row_data(row_data: dict) -> dict:
    if not row_data:
        return {}
    normalized = {}
    for k, v in row_data.items():
        if k is not None:
            val_str = str(v)
            clean_key = str(k).strip().strip(BIDI_BOM_CHARS).strip().lower()
            clean_key = clean_key.replace("\xa0", " ")
            normalized[clean_key] = val_str
            normalized[clean_key.replace("_", " ")] = val_str
            normalized[clean_key.replace(" ", "_")] = val_str

            clean_alnum = re.sub(r"[^\w]", "", clean_key)
            if clean_alnum:
                normalized[clean_alnum] = val_str
    return normalized


def substitute_template(text, row_data, for_pdf=False):
    """Replace {{ COLUMN NAME }} placeholders with row data (case-insensitive)."""
    if not text:
        return text

    row_data_lower = _normalize_row_data(row_data)

    def replacer(match):
        raw_name = match.group(1)
        col_name = (raw_name or "").strip().strip(BIDI_BOM_CHARS).strip().lower()
        val = str(row_data_lower.get(col_name, ""))
        if for_pdf and is_rtl(val):
            return prepare_rtl_text(val)
        return val

    return TEMPLATE_PATTERN.sub(replacer, text)


def substitute_html(html_content, row_data, for_pdf=False):
    """Replace {{ COLUMN NAME }} placeholders in HTML content."""
    return substitute_template(html_content, row_data, for_pdf=for_pdf)


def substitute_docx(docx_content, row_data, for_pdf=False):
    """Replace {{ COLUMN NAME }} placeholders in DOCX (zip + XML manipulation)."""
    try:
        input_zip = zipfile.ZipFile(io.BytesIO(docx_content), "r")
        output_buffer = io.BytesIO()
        output_zip = zipfile.ZipFile(output_buffer, "w", zipfile.ZIP_DEFLATED)
        for item in input_zip.infolist():
            data = input_zip.read(item.filename)
            if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                try:
                    text = data.decode("utf-8", errors="replace")
                    text = substitute_template(text, row_data, for_pdf=for_pdf)
                    data = text.encode("utf-8")
                except Exception:
                    pass
            output_zip.writestr(item, data)
        output_zip.close()
        input_zip.close()
        return output_buffer.getvalue()
    except Exception:
        return docx_content


def substitute_xlsx(xlsx_content, row_data):
    """Replace {{ COLUMN NAME }} placeholders in XLSX (zip + XML manipulation)."""
    try:
        input_zip = zipfile.ZipFile(io.BytesIO(xlsx_content), "r")
        output_buffer = io.BytesIO()
        output_zip = zipfile.ZipFile(output_buffer, "w", zipfile.ZIP_DEFLATED)
        for item in input_zip.infolist():
            data = input_zip.read(item.filename)
            if item.filename.startswith("xl/") and item.filename.endswith(".xml"):
                try:
                    text = data.decode("utf-8", errors="replace")
                    text = substitute_template(text, row_data)
                    data = text.encode("utf-8")
                except Exception:
                    pass
            output_zip.writestr(item, data)
        output_zip.close()
        input_zip.close()
        return output_buffer.getvalue()
    except Exception:
        return xlsx_content


def substitute_pptx(pptx_content, row_data):
    """Replace {{ COLUMN NAME }} placeholders in PPTX (zip + XML manipulation)."""
    try:
        input_zip = zipfile.ZipFile(io.BytesIO(pptx_content), "r")
        output_buffer = io.BytesIO()
        output_zip = zipfile.ZipFile(output_buffer, "w", zipfile.ZIP_DEFLATED)
        for item in input_zip.infolist():
            data = input_zip.read(item.filename)
            if item.filename.startswith("ppt/") and item.filename.endswith(".xml"):
                try:
                    text = data.decode("utf-8", errors="replace")
                    text = substitute_template(text, row_data)
                    data = text.encode("utf-8")
                except Exception:
                    pass
            output_zip.writestr(item, data)
        output_zip.close()
        input_zip.close()
        return output_buffer.getvalue()
    except Exception:
        return pptx_content


def _find_placeholder_font_info(page, full_match, var_name):
    """Find original font family, size, and color for a placeholder from PDF page text dict."""
    try:
        d = page.get_text("dict")
        var_clean = var_name.lower().strip()
        full_clean = full_match.lower().strip()

        best_span = None
        for block in d.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    span_text = span.get("text", "").lower()
                    if full_clean in span_text or var_clean in span_text or "@@" in span_text:
                        best_span = span
                        if full_clean in span_text or var_clean in span_text:
                            break
                if best_span and (full_clean in best_span.get("text", "").lower() or var_clean in best_span.get("text", "").lower()):
                    break
            if best_span and (full_clean in best_span.get("text", "").lower() or var_clean in best_span.get("text", "").lower()):
                break

        if not best_span:
            return None

        font_raw = best_span.get("font", "")
        font_size = float(best_span.get("size", 12.0))
        color_val = best_span.get("color", 0)
        flags = best_span.get("flags", 0)

        clean_font = font_raw.split("+")[-1].lower() if "+" in font_raw else font_raw.lower()

        is_bold = "bold" in clean_font or "heavy" in clean_font or "black" in clean_font or bool(flags & 16)
        is_italic = "italic" in clean_font or "oblique" in clean_font or "slanted" in clean_font or bool(flags & 2)
        is_mono = "courier" in clean_font or "mono" in clean_font or "console" in clean_font or bool(flags & 8)
        is_serif = "times" in clean_font or "serif" in clean_font or "roman" in clean_font or "georgia" in clean_font or "garamond" in clean_font or bool(flags & 4)

        is_underline = "underline" in clean_font or "underlined" in clean_font
        if not is_underline:
            try:
                sb = best_span.get("bbox")
                if sb:
                    for draw in page.get_drawings():
                        dr = draw.get("rect")
                        if dr and dr.height <= 3.5 and abs(dr.y0 - sb[3]) <= 5.0:
                            if max(dr.x0, sb[0]) < min(dr.x1, sb[2]):
                                is_underline = True
                                break
            except Exception:
                pass

        if is_mono:
            fontname = "cobi" if (is_bold and is_italic) else ("cobo" if is_bold else ("coit" if is_italic else "cour"))
        elif is_serif:
            fontname = "tibi" if (is_bold and is_italic) else ("tibo" if is_bold else ("tiit" if is_italic else "tiro"))
        else:
            fontname = "hebi" if (is_bold and is_italic) else ("hebo" if is_bold else ("heit" if is_italic else "helv"))

        if isinstance(color_val, (tuple, list)):
            color_tuple = tuple(color_val)
        elif isinstance(color_val, int):
            r = ((color_val >> 16) & 0xFF) / 255.0
            g = ((color_val >> 8) & 0xFF) / 255.0
            b = (color_val & 0xFF) / 255.0
            color_tuple = (r, g, b)
        else:
            color_tuple = (0.0, 0.0, 0.0)

        return {
            "fontname": fontname,
            "fontsize": font_size,
            "color": color_tuple,
            "font_raw": font_raw,
            "is_bold": is_bold,
            "is_italic": is_italic,
            "is_serif": is_serif,
            "is_mono": is_mono,
            "is_underline": is_underline,
        }
    except Exception:
        return None


def _resolve_replacement_style(val, font_info, align_mode="center", is_arabic=None):
    """Resolve CSS font-family, weight, style, decoration, and text-align for HTMLBox replacement."""
    if is_arabic is None:
        is_arabic = is_rtl(val)
    direction = "rtl" if is_arabic else "ltr"

    if font_info:
        font_size = float(font_info.get("fontsize", 12.0))
        color_to_use = font_info.get("color", (0.0, 0.0, 0.0))
        is_bold = bool(font_info.get("is_bold"))
        is_italic = bool(font_info.get("is_italic"))
        is_serif = bool(font_info.get("is_serif"))
        is_mono = bool(font_info.get("is_mono"))
        is_underline = bool(font_info.get("is_underline"))
        raw_f = str(font_info.get("font_raw", ""))
    else:
        font_size = 14.0
        color_to_use = (0.0, 0.0, 0.0)
        is_bold = False
        is_italic = False
        is_serif = False
        is_mono = False
        is_underline = False
        raw_f = ""

    clean_f = raw_f.split("+")[-1].lower() if "+" in raw_f else raw_f.lower()

    font_weight = "bold" if is_bold else "normal"
    font_style = "italic" if is_italic else "normal"
    text_decoration = "underline" if is_underline else "none"

    if is_arabic:
        # Match Arabic typography to placeholder style:
        # Traditional/Naskh serif style vs modern clean sans-serif vs monospaced
        if is_serif:
            font_family = "'Traditional Arabic', 'Amiri', 'Times New Roman', serif"
        elif is_mono:
            font_family = "'Courier New', monospace"
        else:
            font_family = "'Tahoma', 'Arial', 'Noto Sans Arabic', 'Segoe UI', sans-serif"
    else:
        # Match English typography directly to placeholder family:
        if "courier" in clean_f or is_mono:
            font_family = "'Courier New', Courier, monospace"
        elif "times" in clean_f or "roman" in clean_f or is_serif:
            font_family = "'Times New Roman', Times, Georgia, serif"
        elif "georgia" in clean_f:
            font_family = "Georgia, 'Times New Roman', serif"
        elif "garamond" in clean_f:
            font_family = "Garamond, 'Times New Roman', serif"
        elif "arial" in clean_f:
            font_family = "Arial, Helvetica, sans-serif"
        elif "calibri" in clean_f:
            font_family = "Calibri, Arial, sans-serif"
        elif "tahoma" in clean_f:
            font_family = "Tahoma, Arial, sans-serif"
        elif "segoe" in clean_f:
            font_family = "'Segoe UI', Tahoma, sans-serif"
        elif "helv" in clean_f:
            font_family = "Helvetica, Arial, sans-serif"
        elif clean_f:
            clean_name = raw_f.split("+")[-1]
            font_family = f"'{clean_name}', Arial, sans-serif"
        else:
            font_family = "Arial, Helvetica, sans-serif"

    r_c = int(color_to_use[0] * 255)
    g_c = int(color_to_use[1] * 255)
    b_c = int(color_to_use[2] * 255)
    hex_color = f"#{r_c:02x}{g_c:02x}{b_c:02x}"

    # Determine CSS text-align
    if align_mode == "directional":
        css_align = "right" if is_arabic else "left"
    elif align_mode == "left":
        css_align = "left"
    elif align_mode == "right":
        css_align = "right"
    else:
        css_align = "center"

    style = (
        f"<style>div{{"
        f"font-family:{font_family};"
        f"font-weight:{font_weight};"
        f"font-style:{font_style};"
        f"text-decoration:{text_decoration};"
        f"font-size:{font_size}px;"
        f"color:{hex_color};"
        f"text-align:{css_align};"
        f"margin:0;padding:0;line-height:1;"
        f"}}</style>"
    )
    return {
        "style": style,
        "font_size": font_size,
        "css_align": css_align,
        "direction": direction,
    }


def _find_placeholder_rects(page, full_match, var_name):
    """Find exact bounding rectangles (Rects) for a placeholder on a PDF page.
    
    Handles:
    - Direct exact string search (page.search_for(full_match))
    - Spacing variants (@@ NAME @@, @@NAME@@, etc.)
    - Sub-slice span combinations in PyMuPDF text dict
    - Sub-slice word combinations in PyMuPDF text words
    - Fallback expansion around var_name keyword
    """
    rects = page.search_for(full_match)
    if rects:
        return rects

    var_clean = var_name.strip().strip(BIDI_BOM_CHARS)
    if not var_clean:
        return []
    var_lower = var_clean.lower()

    # Search common spacing variants for @@ @@
    candidates = [
        f"@@ {var_clean} @@",
        f"@@{var_clean}@@",
        f"@@ {var_clean}@@",
        f"@@{var_clean} @@",
        f"@@  {var_clean}  @@",
    ]
    for cand in candidates:
        r = page.search_for(cand)
        if r:
            return r

    # Dict spans matching across line spans (checking all sub-slices)
    try:
        import pymupdf as fitz
        d = page.get_text("dict")
        span_rects = []
        for block in d.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                n_spans = len(spans)
                found_line_rect = False
                for start in range(n_spans):
                    for end in range(start, n_spans):
                        sub_spans = spans[start : end + 1]
                        sub_text = "".join(s.get("text", "") for s in sub_spans)
                        if "@@" in sub_text and var_lower in sub_text.lower():
                            x0 = min(s["bbox"][0] for s in sub_spans)
                            y0 = min(s["bbox"][1] for s in sub_spans)
                            x1 = max(s["bbox"][2] for s in sub_spans)
                            y1 = max(s["bbox"][3] for s in sub_spans)
                            span_rects.append(fitz.Rect(x0, y0, x1, y1))
                            found_line_rect = True
                            break
                    if found_line_rect:
                        break
        if span_rects:
            return span_rects
    except Exception:
        pass

    # Words matching across line words (checking all sub-slices)
    try:
        import pymupdf as fitz
        words = page.get_text("words")
        line_words = {}
        for w in words:
            key = (w[5], w[6])
            line_words.setdefault(key, []).append(w)

        word_rects = []
        for key, l_words in line_words.items():
            n_words = len(l_words)
            found_word_rect = False
            for start in range(n_words):
                for end in range(start, n_words):
                    sub_w = l_words[start : end + 1]
                    sub_text = "".join(w[4] for w in sub_w)
                    sub_text_spaced = " ".join(w[4] for w in sub_w)
                    has_delims = "@@" in sub_text or "@@" in sub_text_spaced
                    if has_delims and var_lower in sub_text.lower():
                        x0 = min(sw[0] for sw in sub_w)
                        y0 = min(sw[1] for sw in sub_w)
                        x1 = max(sw[2] for sw in sub_w)
                        y1 = max(sw[3] for sw in sub_w)
                        word_rects.append(fitz.Rect(x0, y0, x1, y1))
                        found_word_rect = True
                        break
                if found_word_rect:
                    break
        if word_rects:
            return word_rects
    except Exception:
        pass

    # Fallback to var_clean with horizontal padding to cover surrounding {{ }} or @@ @@
    var_rects = page.search_for(var_clean)
    if var_rects:
        import pymupdf as fitz
        expanded = []
        for r in var_rects:
            expanded.append(fitz.Rect(r.x0 - 25.0, r.y0 - 2.0, r.x1 + 25.0, r.y1 + 2.0))
        return expanded

    return []


def _cluster_rects_by_line(rects, line_tolerance=5.0):
    """Cluster bounding rects into lines/groups that form a single text occurrence."""
    if not rects:
        return []
    sorted_rects = sorted(rects, key=lambda r: (r.y0, r.x0))
    groups = []
    current_group = [sorted_rects[0]]
    for r in sorted_rects[1:]:
        avg_y0 = sum(item.y0 for item in current_group) / len(current_group)
        avg_h = sum(item.height for item in current_group) / len(current_group)
        if abs(r.y0 - avg_y0) <= max(line_tolerance, avg_h * 0.5):
            current_group.append(r)
        else:
            groups.append(current_group)
            current_group = [r]
    if current_group:
        groups.append(current_group)
    return groups


def substitute_pdf(pdf_bytes, row_data, lock_pdf=None, owner_password=None, pdf_alignment=None):
    """Replace @@ COLUMN NAME @@ placeholders in PDF streams using PyMuPDF/FPDF2 for complex scripts."""
    try:
        font_path = get_font_path()
        row_data_lower = _normalize_row_data(row_data) if row_data else {}

        should_lock = False
        if lock_pdf is not None:
            should_lock = bool(lock_pdf)
        else:
            try:
                from .settings import get_setting
                val_s = (get_setting("lock_substituted_pdf") or "").strip().lower()
                should_lock = val_s in {"true", "1", "yes", "on"}
            except Exception:
                should_lock = False

        if owner_password is None:
            try:
                from .settings import get_setting
                owner_password = (get_setting("pdf_owner_password") or "").strip() or "MizallaSecureLock"
            except Exception:
                owner_password = "MizallaSecureLock"

        align_mode = (pdf_alignment or "").strip().lower()
        if not align_mode:
            try:
                from .settings import get_setting
                align_mode = (get_setting("pdf_text_alignment") or "center").strip().lower()
            except Exception:
                align_mode = "center"
        if align_mode not in {"center", "directional", "left", "right"}:
            align_mode = "center"

        # Try PyMuPDF (fitz) first for searching, transparent redaction, and direct text insertion
        try:
            import pymupdf as fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            fitz_modified = False

            for page in doc:
                # 1. First, check and handle any interactive Form Field Widgets (AcroForm annotations).
                # Form field widgets are not cleared by page.apply_redactions(), causing the original
                # placeholder to persist alongside the replacement text. We delete matching widgets
                # and render the replacement text directly into the page content stream.
                try:
                    for w in list(page.widgets()):
                        val_str = (w.field_value or "")
                        name_str = (w.field_name or "")
                        has_template = bool(TEMPLATE_PATTERN.search(val_str) or TEMPLATE_PATTERN.search(name_str))

                        name_match_col = None
                        if not has_template and not val_str.strip() and name_str:
                            clean_n = name_str.strip().strip(BIDI_BOM_CHARS).strip().lower()
                            if clean_n in row_data_lower:
                                name_match_col = clean_n

                        if has_template or name_match_col:
                            if name_match_col:
                                replacement = str(row_data_lower.get(name_match_col, ""))
                            else:
                                def _w_repl(m):
                                    rn = m.group(1)
                                    cn = (rn or "").strip().strip(BIDI_BOM_CHARS).strip().lower()
                                    return str(row_data_lower.get(cn, m.group(0)))
                                if TEMPLATE_PATTERN.search(val_str):
                                    replacement = TEMPLATE_PATTERN.sub(_w_repl, val_str)
                                else:
                                    replacement = TEMPLATE_PATTERN.sub(_w_repl, name_str)

                            r = w.rect
                            font_size = float(w.text_fontsize) if (w.text_fontsize and w.text_fontsize > 0) else 14.0

                            # Determine font color
                            tc = w.text_color
                            if isinstance(tc, (list, tuple)) and len(tc) == 3:
                                color_to_use = (float(tc[0]), float(tc[1]), float(tc[2]))
                            elif isinstance(tc, (list, tuple)) and len(tc) == 1:
                                color_to_use = (float(tc[0]), float(tc[0]), float(tc[0]))
                            else:
                                color_to_use = (0.0, 0.0, 0.0)

                            w_font_name = str(w.text_font or "")
                            clean_w_font = w_font_name.lower()
                            w_font_info = {
                                "fontsize": font_size,
                                "color": color_to_use,
                                "font_raw": w_font_name,
                                "is_bold": "bold" in clean_w_font,
                                "is_italic": "italic" in clean_w_font or "oblique" in clean_w_font,
                                "is_serif": "times" in clean_w_font or "serif" in clean_w_font or "roman" in clean_w_font,
                                "is_mono": "courier" in clean_w_font or "mono" in clean_w_font,
                                "is_underline": False,
                            }
                            arabic = is_rtl(replacement)
                            style_data = _resolve_replacement_style(replacement, w_font_info, align_mode=align_mode, is_arabic=arabic)
                            html_text = style_data["style"] + f"<div>{replacement}</div>"
                            css_align = style_data["css_align"]
                            pos_y = r.y0 - (font_size * 0.15)
                            box_height = font_size * 3

                            if css_align == "right":
                                rect_html = fitz.Rect(r.x0, pos_y, r.x1, pos_y + box_height)
                            elif css_align == "left":
                                rect_html = fitz.Rect(r.x0, pos_y, r.x1, pos_y + box_height)
                            else:  # center
                                rect_html = fitz.Rect(r.x0, pos_y, r.x1, pos_y + box_height)

                            # Delete the interactive form widget so the placeholder is permanently erased
                            page.delete_widget(w)

                            if replacement:
                                try:
                                    page.insert_htmlbox(rect_html, html_text)
                                except Exception:
                                    pass

                            fitz_modified = True
                except Exception:
                    pass

                text = page.get_text()
                matches = list(TEMPLATE_PATTERN.finditer(text))
                if not matches:
                    continue

                # Collect all replacements for this page first
                page_replacements = []
                for match in matches:
                    full_match = match.group(0)
                    raw_name = match.group(1)
                    var_name = (raw_name or "").replace("\xa0", " ").strip().strip(BIDI_BOM_CHARS).strip()
                    col_name = var_name.lower()
                    val = str(row_data_lower.get(col_name, ""))
                    font_info = _find_placeholder_font_info(page, full_match, var_name)
                    rects = _find_placeholder_rects(page, full_match, var_name)

                    if rects:
                        page_replacements.append({
                            "full_match": full_match,
                            "var_name": var_name,
                            "col_name": col_name,
                            "val": val,
                            "font_info": font_info,
                            "rects": rects,
                        })

                if not page_replacements:
                    continue

                # Add all redaction annotations for this page (redacting all spans of the placeholder)
                for item in page_replacements:
                    for r in item["rects"]:
                        page.add_redact_annot(r, fill=None)

                # Apply redactions ONCE per page
                page.apply_redactions()

                # Insert replacement texts (once per line cluster to prevent multi-span duplication)
                for item in page_replacements:
                    val = item["val"]
                    rects = item["rects"]
                    font_info = item["font_info"]

                    if not val:
                        continue  # Blank replacement was already redacted

                    arabic = is_rtl(val)
                    style_data = _resolve_replacement_style(val, font_info, align_mode=align_mode, is_arabic=arabic)
                    html_text = style_data["style"] + f"<div>{val}</div>"
                    css_align = style_data["css_align"]
                    font_size = style_data["font_size"]

                    # Cluster rects by line so multi-fragment placeholders receive only 1 insertion
                    clusters = _cluster_rects_by_line(rects)
                    for cluster in clusters:
                        min_x = min(r.x0 for r in cluster)
                        max_x = max(r.x1 for r in cluster)
                        min_y = min(r.y0 for r in cluster)
                        pos_y = min_y - (font_size * 0.15)
                        box_height = font_size * 3

                        if css_align == "right":
                            left_bound = max(0.0, max_x - max(page.rect.width * 0.75, 500.0))
                            rect_html = fitz.Rect(left_bound, pos_y, max_x, pos_y + box_height)
                        elif css_align == "left":
                            rect_html = fitz.Rect(min_x, pos_y, page.rect.width, pos_y + box_height)
                        else:  # center
                            center_x = (min_x + max_x) / 2
                            box_width = max(page.rect.width, 800)
                            pos_x = center_x - (box_width / 2)
                            rect_html = fitz.Rect(pos_x, pos_y, pos_x + box_width, pos_y + box_height)

                        try:
                            page.insert_htmlbox(rect_html, html_text)
                        except Exception:
                            print("Error in htmlbox insertion")
                            pass

                fitz_modified = True

            if fitz_modified:
                save_kwargs = {
                    "deflate": True,
                    "garbage": 4,
                    "clean": True,
                }
                if should_lock:
                    try:
                        doc.bake()
                    except Exception:
                        pass
                    perm = int(
                        fitz.PDF_PERM_PRINT |
                        fitz.PDF_PERM_ACCESSIBILITY
                    )
                    save_kwargs.update({
                        "encryption": fitz.PDF_ENCRYPT_AES_256,
                        "owner_pw": owner_password,
                        "user_pw": "",
                        "permissions": perm,
                    })
                return doc.tobytes(**save_kwargs)
        except Exception as e:
            print("Error in fitz loop:" + str(e))
            pass

        # Fallback to pypdf + FPDF2 if fitz unavailable or found no matches
        from fpdf import FPDF
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        writer = pypdf.PdfWriter()
        modified = False

        for page in reader.pages:
            placeholders = []

            # Process interactive form field widgets in /Annots if present
            if "/Annots" in page:
                try:
                    annots = page["/Annots"]
                    annots_list = annots.get_object() if hasattr(annots, "get_object") else annots
                    if isinstance(annots_list, list):
                        kept_annots = []
                        annots_changed = False
                        for annot_ref in annots_list:
                            annot_obj = annot_ref.get_object() if hasattr(annot_ref, "get_object") else annot_ref
                            subtype = str(annot_obj.get("/Subtype", ""))
                            val_str = str(annot_obj.get("/V") or "")
                            name_str = str(annot_obj.get("/T") or "")

                            has_match = False
                            if subtype == "/Widget":
                                m_val = list(TEMPLATE_PATTERN.finditer(val_str))
                                if m_val:
                                    has_match = True
                                    for m in m_val:
                                        rn = m.group(1)
                                        cn = (rn or "").strip().strip(BIDI_BOM_CHARS).strip().lower()
                                        if cn in row_data_lower:
                                            rect = annot_obj.get("/Rect")
                                            rx0, ry0, rx1, ry1 = [float(x) for x in rect] if rect else (0, 0, 0, 0)
                                            placeholders.append({
                                                "full_match": m.group(0),
                                                "col_name": cn,
                                                "val": str(row_data_lower[cn]),
                                                "x": rx0,
                                                "y": ry0,
                                                "box_w": rx1 - rx0,
                                                "font_size": 14.0,
                                            })
                                elif name_str:
                                    m_name = TEMPLATE_PATTERN.search(name_str)
                                    cn = m_name.group(1).strip().strip(BIDI_BOM_CHARS).strip().lower() if m_name else name_str.strip().lower()
                                    if cn in row_data_lower:
                                        has_match = True
                                        rect = annot_obj.get("/Rect")
                                        rx0, ry0, rx1, ry1 = [float(x) for x in rect] if rect else (0, 0, 0, 0)
                                        placeholders.append({
                                            "full_match": name_str,
                                            "col_name": cn,
                                            "val": str(row_data_lower[cn]),
                                            "x": rx0,
                                            "y": ry0,
                                            "box_w": rx1 - rx0,
                                            "font_size": 14.0,
                                        })
                            if has_match:
                                annots_changed = True
                            else:
                                kept_annots.append(annot_ref)
                        if annots_changed:
                            page[pypdf.generic.NameObject("/Annots")] = pypdf.generic.ArrayObject(kept_annots)
                            modified = True
                except Exception:
                    pass

            def visitor(text, cm, tm, font_dict, font_size):
                for match in TEMPLATE_PATTERN.finditer(text):
                    raw_name = match.group(1)
                    col_name = (raw_name or "").strip().strip(BIDI_BOM_CHARS).strip().lower()
                    if col_name in row_data_lower:
                        val = str(row_data_lower[col_name])
                        placeholders.append({
                            "full_match": match.group(0),
                            "col_name": col_name,
                            "val": val,
                            "x": tm[4],
                            "y": tm[5],
                            "font_size": font_size or 12.0,
                        })

            try:
                page.extract_text(visitor_text=visitor)
            except Exception:
                print("Error in visitor text loop")
                pass

            if placeholders:
                if "/Contents" in page:
                    contents_obj = page["/Contents"]
                    objs = contents_obj.get_object() if not isinstance(contents_obj, list) else contents_obj
                    if not isinstance(objs, list):
                        objs = [objs]
                    for obj_ref in objs:
                        obj = obj_ref.get_object()
                        if hasattr(obj, "get_data"):
                            raw_bytes = obj.get_data()
                            try:
                                text_content = raw_bytes.decode("utf-8")
                                encoding = "utf-8"
                            except UnicodeDecodeError:
                                text_content = raw_bytes.decode("latin-1", errors="replace")
                                encoding = "latin-1"

                            def blank_replacer(m):
                                raw_cname = m.group(1)
                                cname = (raw_cname or "").strip().strip(BIDI_BOM_CHARS).strip().lower()
                                if cname in row_data_lower:
                                    return " " * len(m.group(0))
                                return m.group(0)

                            new_text = TEMPLATE_PATTERN.sub(blank_replacer, text_content)
                            if new_text != text_content:
                                modified = True
                                obj.set_data(new_text.encode(encoding, errors="replace"))

                page_w = float(page.mediabox.width)
                page_h = float(page.mediabox.height)
                fpdf = FPDF(unit="pt", format=(page_w, page_h))
                fpdf.set_auto_page_break(False)
                fpdf.add_page()
                if font_path:
                    fpdf.add_font("MizallaFont", fname=font_path)
                    fpdf.set_font("MizallaFont", size=12)
                else:
                    fpdf.set_font("helvetica", size=12)

                for p in placeholders:
                    val = p["val"]
                    display_text = prepare_rtl_text(val) if is_rtl(val) else val
                    font_size = float(p.get("font_size", 12.0))
                    if font_path:
                        fpdf.set_font("MizallaFont", size=font_size)
                    else:
                        fpdf.set_font("helvetica", size=font_size)
                    fpdf.set_text_color(0, 0, 0)

                    display_w = fpdf.get_string_width(display_text)
                    w_orig = fpdf.get_string_width(p["full_match"])
                    if align_mode == "directional":
                        if is_rtl(val):
                            x_new = p["x"] + w_orig - display_w
                        else:
                            x_new = p["x"]
                    elif align_mode == "left":
                        x_new = p["x"]
                    elif align_mode == "right":
                        x_new = p["x"] + w_orig - display_w
                    else:  # center
                        x_center = p["x"] + (w_orig / 2.0)
                        x_new = x_center - (display_w / 2.0)
                    y_fpdf = page_h - p["y"]

                    fpdf.text(x_new, y_fpdf, display_text)

                overlay_bytes = bytes(fpdf.output())
                overlay_reader = pypdf.PdfReader(io.BytesIO(overlay_bytes))
                page.merge_page(overlay_reader.pages[0])
                modified = True

            writer.add_page(page)

        if modified:
            for p in writer.pages:
                try:
                    p.compress_content_streams()
                except Exception:
                    pass
            if should_lock:
                try:
                    writer.encrypt(
                        user_password="",
                        owner_password=owner_password,
                        permissions_flag=pypdf.constants.UserAccessPermissions.PRINT | pypdf.constants.UserAccessPermissions.EXTRACT_TEXT,
                    )
                except Exception:
                    pass
            out = io.BytesIO()
            writer.write(out)
            return out.getvalue()
        return pdf_bytes
    except Exception as e:
        print("Error in pypdf loop: " + str(e))
        return pdf_bytes


def substitute_attachment(filename, content, row_data, convert_pdf=False, lock_pdf=None, owner_password=None, pdf_alignment=None):
    """Substitute template variables in attachment based on file type (PDF, DOCX, XLSX, PPTX, HTML)."""
    from pathlib import Path
    suffix = Path(filename).suffix.lower()
    if suffix in {".htm", ".html"}:
        sub_html = substitute_html(content.decode("utf-8", errors="replace"), row_data, for_pdf=convert_pdf).encode("utf-8")
        if convert_pdf:
            pdf_bytes = convert_to_pdf(filename, sub_html)
            if pdf_bytes:
                return pdf_bytes
        return sub_html
    elif suffix == ".docx":
        docx_sub = substitute_docx(content, row_data, for_pdf=convert_pdf)
        if convert_pdf:
            pdf_bytes = convert_to_pdf(filename, docx_sub)
            if pdf_bytes:
                return pdf_bytes
        return docx_sub
    elif suffix == ".xlsx":
        return substitute_xlsx(content, row_data)
    elif suffix == ".pptx":
        return substitute_pptx(content, row_data)
    elif suffix == ".pdf":
        return substitute_pdf(content, row_data, lock_pdf=lock_pdf, owner_password=owner_password, pdf_alignment=pdf_alignment)
    else:
        return content


def clean_html(html_content, base_url=None):
    """Clean HTML by inlining CSS, JS, and embedding images as base64."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html_content

    soup = BeautifulSoup(html_content, "html.parser")

    # Inline external stylesheets
    for link in soup.find_all("link", rel="stylesheet"):
        href = link.get("href", "")
        if not href:
            link.decompose()
            continue
        if href.startswith("data:"):
            continue
        if base_url and not href.startswith(("http://", "https://", "//")):
            from urllib.parse import urljoin
            href = urljoin(base_url, href)
        if href.startswith("//"):
            href = "https:" + href
        try:
            import urllib.request
            req = urllib.request.Request(href, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                css_content = resp.read().decode("utf-8", errors="replace")
            style_tag = soup.new_tag("style")
            style_tag.string = css_content
            link.replace_with(style_tag)
        except Exception:
            link.decompose()

    # Inline external scripts
    for script in soup.find_all("script", src=True):
        src = script.get("src", "")
        if not src:
            continue
        if base_url and not src.startswith(("http://", "https://", "//")):
            from urllib.parse import urljoin
            src = urljoin(base_url, src)
        if src.startswith("//"):
            src = "https:" + src
        try:
            import urllib.request
            req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                js_content = resp.read().decode("utf-8", errors="replace")
            script["src"] = None
            script.string = js_content
        except Exception:
            script.decompose()

    # Embed images as base64 data URIs
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        if src.startswith("data:"):
            continue
        if base_url and not src.startswith(("http://", "https://", "//")):
            from urllib.parse import urljoin
            src = urljoin(base_url, src)
        if src.startswith("//"):
            src = "https:" + src
        try:
            import urllib.request
            req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                img_data = resp.read()
                content_type = resp.headers.get("Content-Type", "image/png")
                if ";" in content_type:
                    content_type = content_type.split(";")[0]
            encoded = base64.b64encode(img_data).decode("ascii")
            img["src"] = f"data:{content_type};base64,{encoded}"
        except Exception:
            pass

    return str(soup)


def html_to_pdf(html_content):
    """Convert HTML content to PDF using weasyprint with fpdf2 fallback."""
    try:
        from weasyprint import HTML
        return HTML(string=html_content).write_pdf()
    except Exception:
        pass

    try:
        from fpdf import FPDF

        clean_text = re.sub(r"<[^>]+>", " ", html_content)
        lines = [line.strip() for line in clean_text.splitlines() if line.strip()]

        font_path = get_font_path(clean_text)
        pdf = FPDF(unit="pt", format="letter")
        pdf.add_page()
        if font_path:
            pdf.add_font("MizallaFont", fname=font_path)
            pdf.set_font("MizallaFont", size=12)
        else:
            pdf.set_font("helvetica", size=12)

        for line in lines:
            line_text = prepare_rtl_text(line) if is_rtl(line) else line
            pdf.multi_cell(0, 15, text=line_text)
            pdf.ln(4)

        return bytes(pdf.output())
    except Exception:
        return None


def docx_to_pdf(docx_content):
    """Convert DOCX content to PDF using python-docx + fpdf2."""
    try:
        from docx import Document
        from fpdf import FPDF
    except ImportError:
        return None

    doc = Document(io.BytesIO(docx_content))
    doc_text = " ".join(p.text for p in doc.paragraphs if p.text)
    font_path = get_font_path(doc_text)
    pdf = FPDF(unit="pt", format="letter")
    pdf.add_page()
    if font_path:
        pdf.add_font("MizallaFont", fname=font_path)
        pdf.set_font("MizallaFont", size=12)
    else:
        pdf.set_font("helvetica", size=12)

    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            is_heading = para.style and para.style.name.startswith("Heading")
            size = 16 if is_heading else 12
            if font_path:
                pdf.set_font("MizallaFont", size=size)
            else:
                pdf.set_font("helvetica", size=size)

            line_text = prepare_rtl_text(text) if is_rtl(text) else text
            pdf.multi_cell(0, size * 1.2, text=line_text)
            pdf.ln(4)

    return bytes(pdf.output())


def doc_to_pdf(doc_content):
    """Convert legacy DOC content to PDF using fpdf2."""
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    text = doc_content.decode("utf-8", errors="replace")
    text = "".join(c for c in text if c.isprintable() or c in "\n\r\t")

    font_path = get_font_path(text)
    pdf = FPDF(unit="pt", format="letter")
    pdf.add_page()
    if font_path:
        pdf.add_font("MizallaFont", fname=font_path)
        pdf.set_font("MizallaFont", size=12)
    else:
        pdf.set_font("helvetica", size=12)

    line_text = prepare_rtl_text(text[:5000]) if is_rtl(text[:5000]) else text[:5000]
    pdf.multi_cell(0, 15, text=line_text)
    return bytes(pdf.output())


def convert_to_pdf(filename, content):
    """Convert file content to PDF based on file type."""
    from pathlib import Path
    suffix = Path(filename).suffix.lower()
    if suffix in {".htm", ".html"}:
        return html_to_pdf(content.decode("utf-8", errors="replace"))
    elif suffix == ".docx":
        return docx_to_pdf(content)
    elif suffix == ".doc":
        return doc_to_pdf(content)
    return None


def is_html(filename):
    from pathlib import Path
    return Path(filename).suffix.lower() in {".htm", ".html"}


def is_doc(filename):
    from pathlib import Path
    return Path(filename).suffix.lower() in {".doc", ".docx"}


def is_pdf(filename):
    from pathlib import Path
    return Path(filename).suffix.lower() == ".pdf"


def is_convertible_to_pdf(filename):
    return is_html(filename) or is_doc(filename)


def is_cleanable_html(filename):
    return is_html(filename)
