"""Google Analytics integration client and Data API helper.

Authenticates against Google APIs using the Service Account JSON key
configured in the environment via GOOGLE_ANALYTICS_APIKEY_JSON.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jwt

from .azure_ai import ServiceError, ServiceNotConfigured, credential, tls_verification_disabled
from .settings import get_setting

# Google OAuth2 and Analytics Data API endpoints
GOOGLE_OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_ANALYTICS_DATA_API_BASE = "https://analyticsdata.googleapis.com/v1beta"
GOOGLE_ANALYTICS_ADMIN_API_BASE = "https://analyticsadmin.googleapis.com/v1beta"
ANALYTICS_SCOPES = "https://www.googleapis.com/auth/analytics.readonly"

# Cache token in-memory to avoid signing on every request
_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0}


def get_key_path() -> Optional[Path]:
    """Resolves the Service Account key path relative to the base directory."""
    raw = credential("google_analytics_apikey_json").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        # Relative to project base directory (parent of mizalla/ package)
        base_dir = Path(__file__).resolve().parent.parent
        p = base_dir / raw
    return p if p.exists() else None


def load_service_account_info() -> Dict[str, Any]:
    """Loads and parses the Service Account key JSON file."""
    key_path = get_key_path()
    if not key_path:
        raise ServiceNotConfigured(
            "Google Analytics Service Account JSON key is not configured or file not found. "
            "Please check GOOGLE_ANALYTICS_APIKEY_JSON in .env."
        )
    try:
        with open(key_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise ServiceError(f"Failed to read Service Account key file: {e}") from e


def get_access_token() -> str:
    """Generates an OAuth 2.0 access token via Service Account JWT assertion."""
    now = int(time.time())
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    sa_info = load_service_account_info()
    client_email = sa_info.get("client_email")
    private_key = sa_info.get("private_key")
    token_uri = sa_info.get("token_uri", GOOGLE_OAUTH_TOKEN_URI)

    if not client_email or not private_key:
        raise ServiceError("Service Account JSON key is missing 'client_email' or 'private_key'.")

    payload = {
        "iss": client_email,
        "scope": ANALYTICS_SCOPES,
        "aud": token_uri,
        "exp": now + 3600,
        "iat": now,
    }

    signed_jwt = jwt.encode(payload, private_key, algorithm="RS256")
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": signed_jwt,
    }).encode("utf-8")

    req = urllib.request.Request(token_uri, data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))
            access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 3600)
            _token_cache["token"] = access_token
            _token_cache["expires_at"] = now + expires_in
            return access_token
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        raise ServiceError(f"Failed to obtain Google Analytics access token: {err_msg}") from e
    except Exception as e:
        raise ServiceError(f"Authentication connection error: {e}") from e


def test_connection() -> Dict[str, Any]:
    """Tests connection to Google Analytics and inspects service account metadata."""
    sa_info = load_service_account_info()
    access_token = get_access_token()
    return {
        "status": "connected",
        "client_email": sa_info.get("client_email"),
        "project_id": sa_info.get("project_id"),
        "key_id": sa_info.get("private_key_id"),
        "access_token_active": bool(access_token),
    }


def fetch_metadata(property_id: str = "0") -> Dict[str, Any]:
    """Retrieves Google Analytics standard dimensions and metrics metadata for browsing."""
    access_token = get_access_token()
    url = f"{GOOGLE_ANALYTICS_DATA_API_BASE}/properties/{property_id}/metadata"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        raise ServiceError(f"Google Analytics Data API Error ({e.code}): {err_body}") from e
    except Exception as e:
        raise ServiceError(f"Failed to fetch metadata from Google Analytics: {e}") from e


def run_report(property_id: str, report_spec: Dict[str, Any]) -> Dict[str, Any]:
    """Runs a report query against a Google Analytics 4 property."""
    access_token = get_access_token()
    url = f"{GOOGLE_ANALYTICS_DATA_API_BASE}/properties/{property_id}:runReport"
    data = json.dumps(report_spec).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        try:
            parsed = json.loads(err_body)
            msg = parsed.get("error", {}).get("message", err_body)
        except Exception:
            msg = err_body
        raise ServiceError(f"Google Analytics report query failed ({e.code}): {msg}") from e
    except Exception as e:
        raise ServiceError(f"Report query error: {e}") from e


def discover_properties() -> List[Dict[str, Any]]:
    """Discovers all Google Analytics properties accessible to the Service Account.

    Queries the Google Analytics Admin API accountSummaries endpoint.
    Returns a list of dicts:
        [{"property_id": "12345678", "display_name": "My Site", "account_name": "My Account"}]
    """
    access_token = get_access_token()
    url = f"{GOOGLE_ANALYTICS_ADMIN_API_BASE}/accountSummaries"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        try:
            parsed = json.loads(err_body)
            msg = parsed.get("error", {}).get("message", err_body)
        except Exception:
            msg = err_body
        raise ServiceError(f"Google Analytics Admin API Error ({e.code}): {msg}") from e
    except Exception as e:
        raise ServiceError(f"Failed to query Google Analytics Admin API: {e}") from e

    properties: List[Dict[str, Any]] = []
    account_summaries = data.get("accountSummaries", [])
    for acc in account_summaries:
        acc_name = acc.get("displayName", acc.get("name", "Account"))
        for prop in acc.get("propertySummaries", []):
            raw_prop = prop.get("property", "")
            prop_id = raw_prop.replace("properties/", "").strip()
            display_name = prop.get("displayName", f"Property {prop_id}")
            if prop_id:
                properties.append({
                    "property_id": prop_id,
                    "display_name": display_name,
                    "account_name": acc_name,
                })
    return properties


def fetch_executive_analytics(
    property_id: str,
    start_date: str = "30daysAgo",
    end_date: str = "today"
) -> Dict[str, Any]:
    """Runs executive reporting queries against GA4 Data API for property_id."""
    clean_prop = property_id.replace("properties/", "").strip()
    date_range = [{"startDate": start_date, "endDate": end_date}]

    # 1. Overview KPIs
    kpi_spec = {
        "dateRanges": date_range,
        "metrics": [
            {"name": "screenPageViews"},
            {"name": "sessions"},
            {"name": "newUsers"},
            {"name": "activeUsers"},
            {"name": "bounceRate"},
            {"name": "averageSessionDuration"},
            {"name": "screenPageViewsPerSession"},
        ],
    }
    kpi_res = run_report(clean_prop, kpi_spec)

    page_views = 0
    sessions = 0
    new_users = 0
    active_users = 0
    bounce_rate = 0.0
    avg_session_duration = 0.0
    screen_page_views_per_session = 0.0

    if kpi_res.get("rows"):
        vals = kpi_res["rows"][0].get("metricValues", [])
        if len(vals) >= 7:
            page_views = int(float(vals[0].get("value", 0) or 0))
            sessions = int(float(vals[1].get("value", 0) or 0))
            new_users = int(float(vals[2].get("value", 0) or 0))
            active_users = int(float(vals[3].get("value", 0) or 0))
            raw_bounce = float(vals[4].get("value", 0) or 0)
            bounce_rate = round(raw_bounce * 100 if raw_bounce <= 1.0 else raw_bounce, 1)
            avg_session_duration = round(float(vals[5].get("value", 0) or 0), 1)
            screen_page_views_per_session = round(float(vals[6].get("value", 0) or 0), 2)

    returning_users = max(0, active_users - new_users)

    # 2. Daily breakdown for min / max stats
    daily_spec = {
        "dateRanges": date_range,
        "dimensions": [{"name": "date"}],
        "metrics": [
            {"name": "screenPageViews"},
            {"name": "sessions"},
            {"name": "averageSessionDuration"},
            {"name": "screenPageViewsPerSession"},
        ],
    }
    daily_res = run_report(clean_prop, daily_spec)
    daily_rows = daily_res.get("rows", [])

    pv_per_session_vals: List[float] = []
    session_dur_vals: List[float] = []

    for r in daily_rows:
        mvals = r.get("metricValues", [])
        if len(mvals) >= 4:
            dur = float(mvals[2].get("value", 0) or 0)
            pv_sess = float(mvals[3].get("value", 0) or 0)
            if pv_sess > 0:
                pv_per_session_vals.append(pv_sess)
            if dur > 0:
                session_dur_vals.append(dur)

    min_pv_per_sess = min(pv_per_session_vals) if pv_per_session_vals else screen_page_views_per_session
    max_pv_per_sess = max(pv_per_session_vals) if pv_per_session_vals else screen_page_views_per_session
    min_session_dur = min(session_dur_vals) if session_dur_vals else avg_session_duration
    max_session_dur = max(session_dur_vals) if session_dur_vals else avg_session_duration

    # 3. Top 5 Countries
    countries_spec = {
        "dateRanges": date_range,
        "dimensions": [{"name": "country"}],
        "metrics": [{"name": "activeUsers"}],
        "orderBys": [{"metric": {"metricName": "activeUsers"}, "desc": True}],
        "limit": 5,
    }
    countries_res = run_report(clean_prop, countries_spec)
    top_countries: List[Dict[str, Any]] = []
    total_country_users = 0
    for r in countries_res.get("rows", []):
        c_name = r["dimensionValues"][0].get("value", "Unknown")
        c_count = int(float(r["metricValues"][0].get("value", 0) or 0))
        total_country_users += c_count
        top_countries.append({"country": c_name, "users": c_count})

    for c in top_countries:
        c["percentage"] = round((c["users"] / total_country_users * 100), 1) if total_country_users > 0 else 0.0

    # 4. Pages list for Grid.js
    pages_spec = {
        "dateRanges": date_range,
        "dimensions": [{"name": "pagePath"}, {"name": "pageTitle"}],
        "metrics": [
            {"name": "screenPageViews"},
            {"name": "activeUsers"},
            {"name": "bounceRate"},
        ],
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        "limit": 1000,
    }
    pages_res = run_report(clean_prop, pages_spec)
    pages: List[Dict[str, Any]] = []
    for r in pages_res.get("rows", []):
        p_path = r["dimensionValues"][0].get("value", "/")
        p_title = r["dimensionValues"][1].get("value", p_path)
        p_views = int(float(r["metricValues"][0].get("value", 0) or 0))
        p_users = int(float(r["metricValues"][1].get("value", 0) or 0))
        p_bounce_raw = float(r["metricValues"][2].get("value", 0) or 0)
        p_bounce = round(p_bounce_raw * 100 if p_bounce_raw <= 1.0 else p_bounce_raw, 1)
        pages.append({
            "path": p_path,
            "title": p_title,
            "views": p_views,
            "users": p_users,
            "bounce_rate": f"{p_bounce}%",
        })

    def format_duration(seconds: float) -> str:
        s = int(round(seconds))
        mins, secs = divmod(s, 60)
        return f"{mins}m {secs:02d}s"

    return {
        "property_id": clean_prop,
        "start_date": start_date,
        "end_date": end_date,
        "page_views": page_views,
        "sessions": sessions,
        "new_sessions": new_users,
        "returning_users": returning_users,
        "bounce_rate": bounce_rate,
        "avg_session_duration_sec": avg_session_duration,
        "avg_session_duration_formatted": format_duration(avg_session_duration),
        "page_views_per_session_avg": screen_page_views_per_session,
        "page_views_per_session_min": min_pv_per_sess,
        "page_views_per_session_max": max_pv_per_sess,
        "session_length_min_sec": min_session_dur,
        "session_length_max_sec": max_session_dur,
        "session_length_min_formatted": format_duration(min_session_dur),
        "session_length_max_formatted": format_duration(max_session_dur),
        "top_countries": top_countries,
        "pages": pages,
    }

