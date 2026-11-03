"""GOWA MCP client — JSON-RPC calls to the GOWA WhatsApp MCP server."""

import json
import logging
import urllib.request
import urllib.error

from .settings import get_setting

logger = logging.getLogger("mizalla.gowa")

_mcp_request_id = 0


def _next_id():
    global _mcp_request_id
    _mcp_request_id += 1
    return _mcp_request_id


def get_mcp_base_url() -> str:
    """Get the GOWA MCP base URL from hot settings.

    When MANAGE_GOWA is enabled, defaults to http://127.0.0.1:{GOWA_PORT} unless
    explicitly overridden in AppSetting with a custom non-default value.
    """
    try:
        from flask import current_app, has_app_context

        if has_app_context() and current_app and current_app.config.get("MANAGE_GOWA"):
            port = current_app.config.get("GOWA_PORT", 3000)
            managed_url = f"http://127.0.0.1:{port}"

            from .extensions import db
            from .models import AppSetting

            with db.session.no_autoflush:
                setting = db.session.scalar(db.select(AppSetting).filter_by(key="gowa_mcp_base_url"))
                if setting and setting.value and setting.value.strip():
                    val = setting.value.strip().rstrip("/")
                    if val != "http://127.0.0.1:3000" or port == 3000:
                        return val
            return managed_url
    except Exception:
        pass

    try:
        from .extensions import db
        from .models import AppSetting

        with db.session.no_autoflush:
            setting = db.session.scalar(db.select(AppSetting).filter_by(key="gowa_mcp_base_url"))
            if setting and setting.value and setting.value.strip():
                return setting.value.strip().rstrip("/")
    except Exception:
        pass

    return get_setting("gowa_mcp_base_url", "http://127.0.0.1:3000").rstrip("/")


def check_health(timeout: int = 5) -> dict:
    """Check whether the GOWA server is reachable.

    Issues a plain GET against the configured base URL and treats any 2xx as
    "up". This deliberately does not probe /mcp — that endpoint is POST-only
    and answers 405 to a GET, so it cannot serve as a liveness check.

    Returns a dict with:
        online:      bool
        status_code: int or None
        url:         the base URL that was probed
        error:       human-readable reason, or None when online
    """
    base_url = get_mcp_base_url()
    try:
        req = urllib.request.Request(base_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        logger.warning("GOWA health check HTTP %d at %s", exc.code, base_url)
        return {
            "online": False,
            "status_code": exc.code,
            "url": base_url,
            "error": f"GOWA responded with HTTP {exc.code}.",
        }
    except Exception as exc:
        logger.warning("GOWA health check failed at %s: %s", base_url, exc)
        return {
            "online": False,
            "status_code": None,
            "url": base_url,
            "error": f"Could not reach GOWA at {base_url} ({exc}).",
        }

    online = 200 <= status < 300
    return {
        "online": online,
        "status_code": status,
        "url": base_url,
        "error": None if online else f"GOWA responded with HTTP {status}.",
    }


def call_mcp(tool_name: str, params: dict | None = None, timeout: int = 30) -> dict:
    """Call a GOWA MCP tool and return the result.

    Returns the tool's result dict on success, or {"error": "..."} on failure.
    """
    base_url = get_mcp_base_url()
    url = f"{base_url}/mcp"

    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": params or {},
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            response = json.loads(body)

        if "error" in response:
            error = response["error"]
            logger.error("GOWA MCP error: %s", error)
            return {"error": error.get("message", str(error))}

        result = response.get("result", {})

        # GOWA MCP tools return structuredContent containing JSON dict or list
        if "structuredContent" in result and isinstance(result["structuredContent"], (dict, list)):
            return result["structuredContent"]

        # MCP tools return content array with text blocks
        content = result.get("content", [])
        if content and isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            if text_parts:
                text = "\n".join(text_parts)
                # Try to parse as JSON (some tools return JSON)
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    return {"text": text}

        return result

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logger.error("GOWA MCP HTTP error %d: %s", e.code, error_body)
        if "panicked" in error_body.lower() or "internal_server_error" in error_body.lower():
            return {"error": "WhatsApp device is disconnected or session has expired in GOWA. Please check device pairing."}
        return {"error": f"HTTP {e.code}: {error_body[:200]}"}
    except Exception as exc:
        logger.error("GOWA MCP call failed: %s", exc)
        return {"error": str(exc)}


def get_connection_status(device_id: str | None = None) -> dict:
    """Check GOWA connection status."""
    params = {"action": "status"}
    if device_id:
        params["device_id"] = device_id
    return call_mcp("whatsapp_app", params)


def get_gowa_devices() -> list[dict]:
    """Fetch list of registered devices from GOWA MCP service.

    Parses device records from GOWA status or devices response.
    """
    devices = []
    seen_ids = set()

    def parse_device_item(item):
        if not isinstance(item, dict):
            return None
        dev_id = str(item.get("device_id") or item.get("id") or item.get("wid") or item.get("phone") or "").strip()
        if not dev_id or dev_id in seen_ids or dev_id == "unknown":
            return None
        seen_ids.add(dev_id)
        name = str(item.get("name") or item.get("pushName") or item.get("device_id") or item.get("phone") or f"GOWA Device ({dev_id[:8]})").strip()
        
        is_connected = item.get("is_connected")
        is_logged_in = item.get("is_logged_in")
        raw_status = str(item.get("status") or "").lower().strip()

        if is_connected is False and is_logged_in is False:
            status = "unpaired"
        elif is_connected is False and is_logged_in is True:
            status = "disconnected"
        elif is_connected is True and (is_logged_in is True or is_logged_in is None):
            status = "active"
        elif raw_status in ("active", "connected", "online"):
            status = "active"
        elif raw_status in ("disconnected", "unpaired", "logged_out", "offline"):
            status = raw_status
        elif is_connected is True:
            status = "active"
        elif item.get("status") is None and is_connected is None and is_logged_in is None:
            status = "active"
        else:
            status = "inactive"

        return {"device_id": dev_id, "name": name, "status": status}

    # Probe GOWA status
    status_res = get_connection_status()
    if "error" not in status_res:
        if isinstance(status_res, dict):
            parsed = parse_device_item(status_res)
            if parsed:
                devices.append(parsed)
            raw = status_res.get("devices") or status_res.get("sessions") or status_res.get("data")
            if isinstance(raw, list):
                for item in raw:
                    parsed = parse_device_item(item)
                    if parsed:
                        devices.append(parsed)

    # Fallback to other actions if empty
    for action_name in ("devices", "list", "sessions"):
        if devices:
            break
        res = call_mcp("whatsapp_app", {"action": action_name})
        if "error" not in res:
            raw = res.get("devices") or res.get("sessions") or res.get("data") or (res if isinstance(res, list) else [])
            if isinstance(raw, list):
                for item in raw:
                    parsed = parse_device_item(item)
                    if parsed:
                        devices.append(parsed)

    return devices



def get_login_qr(device_id: str | None = None) -> dict:
    """Get QR code for device login. Returns QR image data."""
    params = {"action": "login_qr"}
    if device_id:
        params["device_id"] = device_id
    return call_mcp("whatsapp_app", params, timeout=60)


def send_text_message(phone: str, message: str, device_id: str | None = None) -> dict:
    """Send a text message via GOWA."""
    params = {"type": "text", "phone": phone, "message": message}
    if device_id:
        params["device_id"] = device_id
    return call_mcp("whatsapp_send", params, timeout=30)


def send_media_message(phone: str, media_type: str, file_url: str,
                       caption: str = "", device_id: str | None = None) -> dict:
    """Send a media message (image, video, audio, document) via GOWA."""
    type_map = {
        "image": "image",
        "video": "video",
        "audio": "audio",
        "document": "document",
    }
    wa_type = type_map.get(media_type, "document")

    params = {"type": wa_type, "phone": phone, "caption": caption}
    if wa_type == "image":
        params["image_url"] = file_url
    elif wa_type == "video":
        params["video_url"] = file_url
    elif wa_type == "audio":
        params["audio_url"] = file_url
    elif wa_type == "document":
        params["file_url"] = file_url

    if device_id:
        params["device_id"] = device_id
    return call_mcp("whatsapp_send", params, timeout=60)
