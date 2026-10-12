from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


def _get_serializer(secret_key: str | None = None) -> URLSafeTimedSerializer:
    key = secret_key or current_app.config["SECRET_KEY"]
    return URLSafeTimedSerializer(
        secret_key=key,
        salt="project-status-secure-link",
    )


def generate_signed_document_token(document_id: int, member_id: int, secret_key: str | None = None) -> str:
    """Generate a URL-safe signed token for a project document download."""
    serializer = _get_serializer(secret_key)
    payload = {
        "doc_id": document_id,
        "mem_id": member_id,
        "action": "doc_download",
    }
    return serializer.dumps(payload)


def verify_signed_document_token(token: str, max_age: int = 3600, secret_key: str | None = None) -> dict | None:
    """
    Verify and decode a signed document token.
    Returns the payload dict {"doc_id": int, "mem_id": int, "action": str} if valid
    and unexpired (within max_age seconds), else None.
    """
    serializer = _get_serializer(secret_key)
    try:
        data = serializer.loads(token, max_age=max_age)
        if isinstance(data, dict) and data.get("action") == "doc_download":
            return data
    except (BadSignature, SignatureExpired):
        pass
    return None
