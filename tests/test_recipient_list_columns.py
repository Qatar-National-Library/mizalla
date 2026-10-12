"""Tests for contact CSV column validation across Bulk Emailer, Bulk SMS, and Bulk Messenger."""

import io
import pytest
from mizalla.recipient_list import read_recipients


def test_email_column_validation_success():
    csv_bytes = b"EMAIL,NAME\nalice@example.com,Alice\nbob@example.com,Bob"
    recipients, rows = read_recipients(io.BytesIO(csv_bytes), "EMAIL")
    assert recipients == ["alice@example.com", "bob@example.com"]
    assert rows[0]["name"] == "Alice"


def test_email_alias_column_validation_success():
    csv_bytes = b"Email_Address,NAME\nalice@example.com,Alice"
    recipients, rows = read_recipients(io.BytesIO(csv_bytes), "EMAIL")
    assert recipients == ["alice@example.com"]


def test_email_column_validation_failure():
    csv_bytes = b"PHONE,NAME\n97455142382,Alice"
    with pytest.raises(ValueError, match="The CSV file must contain a header row with a EMAIL column"):
        read_recipients(io.BytesIO(csv_bytes), "EMAIL")


def test_phone_column_validation_success():
    csv_bytes = b"PHONE,NAME\n97455142382,Alice\n97455142383,Bob"
    recipients, rows = read_recipients(io.BytesIO(csv_bytes), "PHONE")
    assert recipients == ["97455142382", "97455142383"]
    assert rows[0]["name"] == "Alice"


def test_phone_alias_column_validation_success():
    csv_bytes = b"Mobile_Number,NAME\n97455142382,Alice"
    recipients, rows = read_recipients(io.BytesIO(csv_bytes), "PHONE")
    assert recipients == ["97455142382"]


def test_phone_column_validation_failure():
    csv_bytes = b"EMAIL,NAME\nalice@example.com,Alice"
    with pytest.raises(ValueError, match="The CSV file must contain a header row with a PHONE column"):
        read_recipients(io.BytesIO(csv_bytes), "PHONE")


def test_empty_data_under_required_column():
    csv_bytes = b"EMAIL,NAME\n,Alice\n,Bob"
    with pytest.raises(ValueError, match="No valid data was found under the EMAIL column"):
        read_recipients(io.BytesIO(csv_bytes), "EMAIL")
