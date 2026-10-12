"""Service Audit Logger — records complete audit trails for every service call.

Records:
- Who made the call (actor_id, actor_name, actor_email)
- When was the call made (created_at timestamp)
- Values of the request (request_data)
- Response values (response_data)
- Service key, action, status, and duration
"""

import json
from datetime import datetime, timezone
from flask import current_app, request
from flask_login import current_user

from .extensions import db
from .models import FEATURE_LABELS, ServiceAuditLog


def _sanitize_value(val, max_len=4000) -> str:
    """Safely convert any request or response object into a readable string summary."""
    if val is None:
        return ""
    if isinstance(val, (bytes, bytearray)):
        return f"<binary data: {len(val):,} bytes>"
    if isinstance(val, (int, float, bool)):
        return str(val)
    if isinstance(val, str):
        if len(val) > max_len:
            return val[:max_len] + f" ... [{len(val):,} chars truncated]"
        return val
    try:
        raw = json.dumps(val, default=str, ensure_ascii=False)
        if len(raw) > max_len:
            return raw[:max_len] + f" ... [{len(raw):,} chars truncated]"
        return raw
    except Exception:
        s = str(val)
        if len(s) > max_len:
            return s[:max_len] + f" ... [{len(s):,} chars truncated]"
        return s


def record_service_audit(
    service_key: str,
    action: str,
    request_data: dict | str | None,
    response_data: dict | str | None,
    actor=None,
    status: str = "SUCCESS",
    duration_ms: int | None = None,
) -> ServiceAuditLog | None:
    """Record a complete audit trail entry for a service call."""
    if actor is None:
        try:
            if current_user and current_user.is_authenticated:
                actor = current_user
        except Exception:
            actor = None

    actor_id = getattr(actor, "id", None)
    actor_name = getattr(actor, "name", "System/Anonymous")
    actor_email = getattr(actor, "email", "")

    # Fallback for API calls if actor is anonymous but API key was used
    if actor_id is None and request:
        auth_header = request.headers.get("Authorization") or request.headers.get("X-API-Key")
        if auth_header:
            actor_name = "API Key Client"

    service_name = FEATURE_LABELS.get(service_key, service_key.replace("_", " ").title())

    log_entry = ServiceAuditLog(
        service_key=service_key,
        service_name=service_name,
        action=action,
        actor_id=actor_id,
        actor_name=actor_name,
        actor_email=actor_email,
        request_data=_sanitize_value(request_data),
        response_data=_sanitize_value(response_data),
        status=status,
        duration_ms=duration_ms,
        created_at=datetime.now(timezone.utc),
    )
    try:
        db.session.add(log_entry)
        db.session.commit()
        return log_entry
    except Exception as exc:
        try:
            current_app.logger.exception("Failed to write ServiceAuditLog: %s", exc)
        except Exception:
            pass
        db.session.rollback()
        return None
