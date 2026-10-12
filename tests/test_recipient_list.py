"""The recipient-list parser shared by the three campaign composers.

These tests exist mostly because the format changed. When all three composers
read Excel workbooks, each reader was tested only for the SMS one, and the
emailer and WhatsApp readers had no upload coverage at all - so the two services
silently accepted a format nobody had ever exercised. Parsing is one function now,
so it is one module of tests, and the per-service wrappers are checked for the
column each one requires.
"""

import io
from pathlib import Path

import pytest

from mizalla.bulk_email import read_recipients as read_email_list
from mizalla.bulk_sms import read_recipients as read_phone_list
from mizalla.recipient_list import (
    EMAIL_LIST,
    PHONE_LIST,
    decode,
    read_recipients,
    validate_upload,
)
from mizalla.whatsapp import read_recipients as read_whatsapp_list


def csv_bytes(text, encoding="utf-8"):
    return io.BytesIO(text.encode(encoding))


# ─── What the composers ask for ──────────────────────────────────────────────

def test_the_emailer_requires_an_email_column():
    with pytest.raises(ValueError, match="EMAIL"):
        read_email_list(csv_bytes("PHONE\n+97411111111\n"))


def test_the_two_phone_services_require_a_phone_column():
    for reader in (read_phone_list, read_whatsapp_list):
        with pytest.raises(ValueError, match="PHONE"):
            reader(csv_bytes("EMAIL\na@b.com\n"))


def test_all_three_composers_share_one_parser():
    """A fix to the reader has to reach the emailer and WhatsApp too.

    The three used to carry identical copies that drifted; the WhatsApp copy had
    already fallen behind on error wording when the SMS one was written.
    """
    from mizalla import bulk_email, bulk_sms, recipient_list, whatsapp

    for module in (bulk_email, bulk_sms, whatsapp):
        assert module._read_recipients is recipient_list.read_recipients, module.__name__


def test_the_emailer_and_a_phone_service_read_the_same_list_identically():
    rows = [
        ["PHONE", "EMAIL", "NAME"],
        ["+97411111111", "amina@example.com", "Amina"],
        ["+97422222222", "bilal@example.com", "Bilal"],
    ]
    text = "\n".join(",".join(row) for row in rows) + "\n"

    phone_numbers, phone_rows = read_phone_list(csv_bytes(text))
    emails, email_rows = read_email_list(csv_bytes(text))

    assert phone_numbers == ["+97411111111", "+97422222222"]
    assert emails == ["amina@example.com", "bilal@example.com"]
    # Same table, so the substitution data must be identical in both.
    assert phone_rows == email_rows


# ─── The composers' copy ─────────────────────────────────────────────────────

def _template(name):
    from mizalla import bulk_sms

    path = Path(bulk_sms.__file__).parent / "templates" / name
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("template,field", [
    ("bulk_email.html", "email_list"),
    ("whatsapp.html", "phone_list"),
    ("bulk_sms.html", "phone_list"),
])
def test_each_composer_offers_csv_in_its_file_picker(template, field):
    """The picker has to match what the route accepts, or the file cannot be chosen."""
    import re

    match = re.search(rf'<input[^>]*name="{field}"[^>]*>', _template(template))
    assert match, f"{template} has no {field} input"
    accept = re.search(r'accept="([^"]*)"', match.group(0))
    assert accept, f"{template} does not restrict {field} by type"
    assert ".csv" in accept.group(1)
    # Attachments are still spreadsheets, but the *list* is not: an .xlsx offered
    # here is a file the route then refuses by name.
    assert ".xlsx" not in accept.group(1) and ".xlsm" not in accept.group(1)


@pytest.mark.parametrize("template", ["bulk_email.html", "whatsapp.html", "bulk_sms.html"])
def test_no_composer_still_calls_its_list_a_workbook(template):
    """Stale copy is the failure mode here: the parser changes, the label doesn't.

    Anchored on ``drop-zone-list`` rather than on the ``drop-zone`` class, which
    the attachments picker shares - and in two templates it comes first, so
    matching the class picks up the attachment copy instead. Attachments are
    legitimately still spreadsheets, as are the exports.
    """
    import re

    zone = re.search(r'id="drop-zone-list".*?</label>', _template(template), re.S)
    assert zone, f"{template} has no recipient drop zone"
    text = zone.group(0).lower()
    for stale in ("workbook", "excel", ".xlsx", ".xlsm"):
        assert stale not in text, f"{template} still says {stale!r} about its recipient list"


# ─── Filename validation ─────────────────────────────────────────────────────

def test_a_csv_filename_is_accepted_in_any_case():
    for filename in ("list.csv", "LIST.CSV", "My List.Csv"):
        assert validate_upload(filename, PHONE_LIST) == "", filename


def test_a_spreadsheet_filename_is_refused_by_name():
    """The message has to say what is wanted, not just that something is wrong."""
    for filename in ("list.xlsx", "list.xlsm", "list.xls", "list.csv.txt"):
        error = validate_upload(filename, PHONE_LIST)
        assert error == "The phone list must be a .csv file.", filename


def test_a_missing_filename_asks_for_the_right_control():
    assert validate_upload("", PHONE_LIST) == "Choose a CSV phone list."
    assert validate_upload("", EMAIL_LIST) == "Choose a CSV email list."


# ─── Decoding ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "cp1252"])
def test_the_encodings_excel_writes_all_decode(encoding):
    assert decode("PHONE,Søren\n".encode(encoding)) == "PHONE,Søren\n"


def test_a_byte_order_mark_never_reaches_the_first_column_name():
    """A BOM left on the header would hide the column the parser is looking for."""
    recipients, rows = read_phone_list(
        csv_bytes("PHONE,NAME\n+97411111111,Amina\n", encoding="utf-8-sig")
    )
    assert recipients == ["+97411111111"]
    assert sorted(rows[0]) == ["name", "phone"]


def test_a_utf32_file_is_not_decoded_as_utf16():
    """UTF-32 LE begins with the UTF-16 LE mark, so order decides correctness."""
    assert decode("PHONE,NAME\n".encode("utf-32")) == "PHONE,NAME\n"


def test_an_unassigned_byte_does_not_reject_the_whole_list():
    """cp1252 leaves five bytes undefined; one stray byte must not lose the list."""
    recipients, rows = read_phone_list(io.BytesIO(b"PHONE,NAME\n+97411111111,A\x81B\n"))
    assert recipients == ["+97411111111"]
    assert rows[0]["name"].startswith("A")


# ─── Delimiter detection ─────────────────────────────────────────────────────

@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
def test_each_supported_delimiter_is_detected_from_the_header(delimiter):
    text = f"PHONE{delimiter}NAME\n+97411111111{delimiter}Amina\n"
    recipients, rows = read_phone_list(csv_bytes(text))
    assert recipients == ["+97411111111"]
    assert rows[0]["name"] == "Amina"


def test_a_semicolon_inside_a_value_is_not_mistaken_for_the_delimiter():
    """Comma wins the first attempt, so a semicolon in data never splits a field."""
    recipients, rows = read_phone_list(csv_bytes("PHONE,NOTE\n+97411111111,a;b;c\n"))
    assert recipients == ["+97411111111"]
    assert rows[0]["note"] == "a;b;c"


def test_a_missing_column_names_the_delimiters_that_were_tried():
    """The member has to know what was looked for to know what to change."""
    with pytest.raises(ValueError) as caught:
        read_phone_list(csv_bytes("MOBILE,NAME\n+97411111111,Amina\n"))
    message = str(caught.value)
    assert "PHONE" in message and "commas" in message and "semicolons" in message


# ─── Rows and values ─────────────────────────────────────────────────────────

def test_a_number_longer_than_a_float_holds_exactly():
    """The reason this format was preferred over a spreadsheet.

    ``97411111111`` opened in Excel reads back as ``97411111111.0``, and one long
    enough to leave a float's exact integer range is silently rounded - a
    corrupted phone number sent to a real person.
    """
    number = "9" * 17
    recipients, _ = read_phone_list(csv_bytes(f"PHONE\n{number}\n"))
    assert recipients == [number]


def test_a_leading_zero_reference_code_survives():
    recipients, rows = read_phone_list(csv_bytes("PHONE,CODE\n+97411111111,00123\n"))
    assert rows[0]["code"] == "00123"


def test_a_quoted_value_keeps_its_comma_and_newline():
    recipients, rows = read_phone_list(
        csv_bytes('PHONE,NOTE\n+97411111111,"line one, line two"\n')
    )
    assert recipients == ["+97411111111"]
    assert rows[0]["note"] == "line one, line two"


def test_blank_and_trailing_lines_are_skipped():
    recipients, rows = read_phone_list(
        csv_bytes("PHONE,NAME\n+97411111111,Amina\n\n\n+97422222222,Bilal\n\n")
    )
    assert recipients == ["+97411111111", "+97422222222"]
    assert len(rows) == 2


def test_a_row_missing_the_trailing_columns_is_still_deliverable():
    """A ragged row means those columns are absent, not that the row is broken."""
    recipients, rows = read_phone_list(csv_bytes("PHONE,NAME,CODE\n+97411111111\n"))
    assert recipients == ["+97411111111"]
    assert rows[0]["phone"] == "+97411111111"
    assert rows[0]["name"] == "" and rows[0]["code"] == ""


def test_rows_stay_paired_with_their_recipient():
    """Substitution is positional; a drifted pair messages the wrong person."""
    recipients, rows = read_phone_list(
        csv_bytes("PHONE,NAME\n+97411111111,Amina\n+97422222222,Bilal\n")
    )
    assert [row["phone"] for row in rows] == recipients


def test_the_column_name_is_matched_case_insensitively_and_trimmed():
    recipients, _ = read_phone_list(csv_bytes("  Phone , Name\n+97411111111,Amina\n"))
    assert recipients == ["+97411111111"]


def test_an_empty_file_is_reported_as_empty():
    with pytest.raises(ValueError, match="empty"):
        read_phone_list(io.BytesIO(b""))
    with pytest.raises(ValueError, match="empty"):
        read_phone_list(io.BytesIO(b"   \n  \n"))


def test_a_blank_line_above_the_header_is_tolerated():
    """A hand-edited file often gains a leading newline; the header is still found."""
    recipients, rows = read_phone_list(csv_bytes("\n\nPHONE,NAME\n+97411111111,Amina\n"))
    assert recipients == ["+97411111111"]
    assert rows[0]["name"] == "Amina"


def test_a_header_with_no_data_rows_yields_nothing_to_send():
    recipients, rows = read_phone_list(csv_bytes("PHONE,NAME\n"))
    assert recipients == [] and rows == []
