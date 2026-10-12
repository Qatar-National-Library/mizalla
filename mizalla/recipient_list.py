"""Reading the recipient list the three campaign composers take.

Bulk Emailer, Bulk Messenger and Bulk SMS all take the same shape of input: an
uploaded table with a header row, one column naming the destination (``EMAIL`` or
``PHONE``), and any number of further columns that become ``{{ COLUMN }}`` values
in the message. That shape is parsed once here rather than three times, so a fix
to the delimiter, the encoding or the error text reaches all three at once.

These lists used to be Excel workbooks. CSV is the better fit for this input,
for reasons beyond the format itself:

* A phone number in a spreadsheet is a *number*, so ``97411111111`` reads back
  as ``97411111111.0``, and a number long enough to leave a float's exact
  integer range is silently rounded - a corrupted number is sent to a real
  person. Every value in a CSV is text, so what was typed is what is sent.
* Dates, codes and reference numbers come back as the characters written rather
  than as Excel's formatted types, so ``00123`` stays ``00123``.
* It is what the member's other tools emit without a conversion step.

The three readers also fail the same ways, so they share these messages. A member
who uploads a phone list to the Emailer is told which column is missing, not
that the file is unreadable.
"""

import codecs
import csv
import io
from pathlib import Path

#: The only extension a recipient list may have.
ALLOWED_SUFFIXES = {".csv"}

#: Delimiters tried in order. Comma first because that is what "CSV" means to
#: most people; the other two cover Excel exports made where the regional list
#: separator is a semicolon, and tab-separated files renamed to ``.csv``.
#: Comma winning the first attempt is what keeps a semicolon *inside* a field
#: from being mistaken for a delimiter.
DELIMITERS = (",", ";", "\t")

#: How the readers name the control in an error message, so the text matches
#: what the member just clicked.
EMAIL_LIST = "email list"
PHONE_LIST = "phone list"

#: Byte-order marks, widest encoding first. UTF-32 LE begins with the UTF-16 LE
#: mark, so the 32-bit codecs have to be tried first or a UTF-32 file decodes as
#: UTF-16 with a null between every character.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode(content: bytes) -> str:
    """Decode uploaded CSV bytes to text.

    Excel is the main way these files get written and it is inconsistent about
    encoding: "CSV UTF-8" prepends a byte-order mark, plain "CSV (Comma
    delimited)" writes the machine's own code page, and anything round-tripped
    through Word or an old tool may be UTF-16. Rather than demand one encoding
    and reject the other three, all of them are accepted.
    """
    for bom, encoding in _BOMS:
        if content.startswith(bom):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                break
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        # cp1252 is what a Windows "CSV (Comma delimited)" export actually is.
        # It leaves five bytes unassigned, so replacement keeps one stray byte
        # from turning a whole list into an upload error.
        return content.decode("cp1252", errors="replace")


def validate_upload(filename: str, list_label: str) -> str:
    """The error for a recipient list that is not a CSV, or ``""`` when it is."""
    if not filename:
        return f"Choose a CSV {list_label}."
    if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
        return f"The {list_label} must be a .csv file."
    return ""


COLUMN_ALIASES = {
    "EMAIL": {"EMAIL", "E-MAIL", "E_MAIL", "EMAIL_ADDRESS", "MAIL", "EMAIL ADDRESS"},
    "PHONE": {"PHONE", "PHONE_NUMBER", "PHONE NUMBER", "MOBILE_NUMBER", "MOBILE NUMBER", "CELL", "TELEPHONE"},
}


def _clean_header(val: str) -> str:
    bidi_bom_chars = "\ufeff\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    return str(val or "").strip().strip(bidi_bom_chars).strip().upper()


def _find_column(headers: list, required: str):
    """Index of the column headed ``required`` or its aliases, matched case-insensitively."""
    required_clean = _clean_header(required)
    valid_names = COLUMN_ALIASES.get(required_clean, {required_clean})
    return next(
        (index for index, value in enumerate(headers)
         if _clean_header(value) in valid_names),
        None,
    )


def read_recipients(file_storage, required_column: str):
    """Read an uploaded CSV and return ``(recipients, rows)``.

    ``recipients`` is the destination column's values in file order, with blank
    rows dropped. ``rows`` holds one dict per recipient keyed by lower-cased
    column name, for ``{{ COLUMN }}`` substitution.

    The two lists are parallel - row *i* describes recipient *i* - because a
    campaign pairs them by position, and a substitution that drifted onto the
    wrong person would be worse than a visible error.

    Raises :class:`ValueError` with text a member can act on, which the routes
    hand straight back as the response.
    """
    raw = file_storage.read()
    text = raw if isinstance(raw, str) else decode(raw)
    if not text.strip():
        raise ValueError("The CSV file is empty.")

    # Choose the delimiter from the header row alone, then parse the file once.
    # Deciding from the header rather than csv.Sniffer matters: Sniffer guesses
    # from statistical patterns across the whole file and is wrong often enough
    # on a three-column list to reject files that are perfectly fine. A header
    # that contains the required column is proof the delimiter is right,
    # whatever the data looks like.
    #
    # The header is the first row with anything in it, not row zero, because a
    # hand-edited file often picks up a leading blank line.
    def is_blank(row):
        return not any(str(cell).strip() for cell in row)

    header_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = None
    for candidate in DELIMITERS:
        header = next(csv.reader([header_line], delimiter=candidate), [])
        if not is_blank(header) and _find_column(header, required_column) is not None:
            delimiter = candidate
            break

    if delimiter is None:
        raise ValueError(
            f"The CSV file must contain a header row with a {required_column} column. "
            "Check the first row, and that the file is separated by commas, "
            "semicolons or tabs."
        )

    table = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    start = next((i for i, row in enumerate(table) if not is_blank(row)), 0)
    column = _find_column(table[start], required_column)
    bidi_bom_chars = "\ufeff\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    keys = [str(header or "").strip().strip(bidi_bom_chars).strip().lower() for header in table[start]]
    recipients = []
    rows = []
    data_rows = table[start + 1:]
    if not data_rows:
        return recipients, rows

    for row in data_rows:
        if is_blank(row):
            continue  # a blank line, or the blank tail most exports carry
        address = str(row[column]).strip() if column < len(row) else ""
        if not address:
            continue
        recipients.append(address)
        # A short row means the trailing columns are simply absent, which the
        # substitution helper already treats as an empty value. Extra cells past
        # the last header are ignored, as a column with no name cannot be
        # referenced from a template anyway.
        rows.append({
            keys[position]: row[position] if position < len(row) else ""
            for position in range(len(keys))
            if keys[position]
        })

    if not recipients:
        raise ValueError(f"No valid data was found under the {required_column} column.")

    if hasattr(file_storage, "seek"):
        try:
            file_storage.seek(0)
        except Exception:
            pass

    return recipients, rows
