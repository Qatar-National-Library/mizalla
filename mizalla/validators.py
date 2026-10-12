"""Shared input validation helpers for the web routes and the REST API.

Every helper raises ``ValueError`` with a caller-facing message. Web routes
flash the message; the REST API turns it into a 400 response.
"""

from datetime import date, time


def bounded_int(value, label: str, minimum: int, maximum: int) -> int:
    """Coerce value to an int inside [minimum, maximum]."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if not minimum <= number <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return number


def optional_date(value, label: str) -> date | None:
    """Parse an ISO date string. An empty or missing value yields None."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a valid date.") from None


def optional_time(value, label: str) -> time | None:
    """Parse an ISO time string. An empty or missing value yields None."""
    if not value:
        return None
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a valid time.") from None


def is_valid_email(email: str) -> bool:
    """Accept a single, whitespace-free address with a non-empty local and domain."""
    return (
        bool(email)
        and email.count("@") == 1
        and not any(char.isspace() for char in email)
        and all(email.split("@"))
    )


def parse_email_list(raw: str) -> tuple[list[str], list[str]]:
    """Split comma, semicolon, or whitespace separated addresses.

    Returns ``(valid, invalid)`` with addresses lowercased and de-duplicated
    while preserving input order.
    """
    valid: list[str] = []
    invalid: list[str] = []
    for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
        candidate = chunk.strip().lower()
        if not candidate:
            continue
        if is_valid_email(candidate):
            if candidate not in valid:
                valid.append(candidate)
        elif candidate not in invalid:
            invalid.append(candidate)
    return valid, invalid
