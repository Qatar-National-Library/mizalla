"""Google Analytics View-Only Service Blueprint.

Provides authenticated members with access to Google Analytics metrics, dimensions,
and preliminary data exploration using the configured Service Account key.
"""

from __future__ import annotations

from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .azure_ai import ServiceError, ServiceNotConfigured, service_status
from .feature_access import has_feature_access
from . import google_analytics
from .models import FEATURE_ANALYTICS, FEATURE_LABELS, GoogleAnalyticsProperty
from .service_audit import record_service_audit

bp = Blueprint("analytics", __name__, url_prefix="/apps/analytics")


def analytics_required(view):
    """Refuse access unless the member has been granted the Google Analytics feature.

    Administrators always pass has_feature_access so they can configure and verify
    the service before rollout.
    """
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not has_feature_access(current_user, FEATURE_ANALYTICS):
            return render_template(
                "error.html",
                title="Not permitted",
                message=(
                    f"You do not have access to {FEATURE_LABELS[FEATURE_ANALYTICS]}. "
                    "An administrator grants this service from Members."
                ),
            ), 403
        return view(*args, **kwargs)

    return wrapped


@bp.route("/")
@analytics_required
def index():
    """Main view-only Google Analytics executive dashboard."""
    key_path = google_analytics.get_key_path()
    status = service_status("analytics")

    connection_info = None
    metadata_error = None
    report_error = None
    report_data = None

    # Retrieve accessible properties for the current user
    accessible_properties = GoogleAnalyticsProperty.accessible_properties_for(current_user)

    # Date range selection: 7d, 30d (default), 90d, custom
    date_preset = request.args.get("date_preset", "30d").strip()
    custom_start = request.args.get("start_date", "").strip()
    custom_end = request.args.get("end_date", "").strip()

    if date_preset == "7d":
        start_date = "7daysAgo"
        end_date = "today"
    elif date_preset == "90d":
        start_date = "90daysAgo"
        end_date = "today"
    elif date_preset == "custom" and custom_start and custom_end:
        start_date = custom_start
        end_date = custom_end
    else:
        date_preset = "30d"
        start_date = "30daysAgo"
        end_date = "today"

    # Property selection
    requested_prop_id = request.args.get("property_id", "").strip().replace("properties/", "")
    selected_property = None

    if requested_prop_id:
        cand = GoogleAnalyticsProperty.query.filter_by(property_id=requested_prop_id).first()
        if cand and cand.can_access(current_user):
            selected_property = cand
        elif cand and not cand.can_access(current_user):
            flash("You do not have permission to view that Google Analytics property.", "error")
            selected_property = accessible_properties[0] if accessible_properties else None
        else:
            if current_user.is_admin:
                selected_property = GoogleAnalyticsProperty(property_id=requested_prop_id, display_name=f"Property {requested_prop_id}")
            else:
                selected_property = accessible_properties[0] if accessible_properties else None
    elif accessible_properties:
        selected_property = accessible_properties[0]

    if status["configured"] and key_path:
        try:
            connection_info = google_analytics.test_connection()
            if selected_property:
                try:
                    report_data = google_analytics.fetch_executive_analytics(
                        selected_property.property_id,
                        start_date=start_date,
                        end_date=end_date,
                    )
                except Exception as ex:
                    report_error = str(ex)
        except (ServiceNotConfigured, ServiceError) as e:
            metadata_error = str(e)
        except Exception as e:
            metadata_error = f"Unexpected error connecting to Google Analytics: {e}"

    record_service_audit(
        service_key=FEATURE_ANALYTICS,
        action="view_executive_dashboard",
        request_data={
            "property_id": selected_property.property_id if selected_property else None,
            "date_preset": date_preset,
            "start_date": start_date,
            "end_date": end_date,
        },
        response_data={
            "page_views": report_data.get("page_views") if report_data else None,
            "error": metadata_error or report_error,
        },
        status="SUCCESS" if not (metadata_error or report_error) else "FAILURE",
    )

    return render_template(
        "analytics.html",
        configured=status["configured"],
        missing=status["missing"],
        key_file_exists=bool(key_path),
        key_filename=key_path.name if key_path else None,
        connection_info=connection_info,
        metadata_error=metadata_error,
        report_error=report_error,
        accessible_properties=accessible_properties,
        selected_property=selected_property,
        date_preset=date_preset,
        start_date=start_date,
        end_date=end_date,
        custom_start=custom_start,
        custom_end=custom_end,
        report_data=report_data,
    )

