"""Feedback and suggestion routes for utility applications.

Logged-in members can rate and suggest improvements to utility apps they have
access to, or submit requests for entirely new utility apps.
"""

from datetime import datetime, timezone
from flask import Blueprint, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from .extensions import db
from .feature_access import has_feature_access
from .models import (
    FEATURE_KEYS,
    FEATURE_LABELS,
    FEEDBACK_STATUS_ACTIVE,
    FEEDBACK_TARGET_EXISTING,
    FEEDBACK_TARGET_NEW,
    UtilityAppFeedback,
)

bp = Blueprint("feedback", __name__, url_prefix="/apps/feedback")


@bp.get("")
@bp.get("/")
@login_required
def index():
    """Route to the feedback interface.

    Admins are directed to the moderation dashboard. Regular members
    are directed to the main page with the feedback modal open.
    """
    if current_user.is_admin:
        return redirect(url_for("admin.feedback_dashboard"))
    return redirect(url_for("main.project_list", open_feedback="true"))


@bp.get("/accessible-apps")
@login_required
def accessible_apps():
    """Return the list of utility applications the signed-in user has access to.

    Includes any existing rating & comments the member has already submitted for
    each app, allowing the rating modal to pre-populate.
    """
    apps = []
    # Fetch existing feedback submissions by this member
    user_feedbacks = {
        fb.feature_key: fb
        for fb in UtilityAppFeedback.query.filter_by(
            member_id=current_user.id, target_type=FEEDBACK_TARGET_EXISTING
        ).all()
        if fb.feature_key
    }

    for key in FEATURE_KEYS:
        if has_feature_access(current_user, key):
            fb = user_feedbacks.get(key)
            apps.append(
                {
                    "feature_key": key,
                    "label": FEATURE_LABELS.get(key, key),
                    "current_rating": fb.rating if fb else None,
                    "current_comments": fb.comments if fb else "",
                    "updated_at": fb.updated_at.strftime("%Y-%m-%d %H:%M") if fb and fb.updated_at else None,
                }
            )

    return jsonify(apps=apps)


@bp.post("/submit")
@login_required
def submit_feedback():
    """Submit a rating & improvement comment for an existing utility app, or suggest a new app."""
    target_type = request.form.get("target_type", "").strip().lower()
    comments = request.form.get("comments") or (
        request.form.get("comments_new", "") if target_type == FEEDBACK_TARGET_NEW else request.form.get("comments_existing", "")
    )
    comments = comments.strip()

    if target_type == FEEDBACK_TARGET_EXISTING:
        feature_key = request.form.get("feature_key", "").strip()
        if not feature_key or feature_key not in FEATURE_KEYS:
            return jsonify(error="Select a valid utility application."), 400

        if not has_feature_access(current_user, feature_key):
            return jsonify(error="You do not have access to rate this utility application."), 403

        raw_rating = request.form.get("rating", "").strip()
        try:
            rating = int(raw_rating)
            if rating < 1 or rating > 10:
                raise ValueError()
        except (ValueError, TypeError):
            return jsonify(error="Rating must be an integer between 1 and 10."), 400

        app_title = FEATURE_LABELS.get(feature_key, feature_key)

        # Check if user already submitted feedback for this app (1 current rating per user per app)
        feedback = UtilityAppFeedback.query.filter_by(
            member_id=current_user.id,
            target_type=FEEDBACK_TARGET_EXISTING,
            feature_key=feature_key,
        ).first()

        if feedback:
            feedback.rating = rating
            feedback.comments = comments
            feedback.app_title = app_title
            feedback.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            return jsonify(
                message=f"Thank you! Your rating ({rating}/10) for {app_title} has been updated.",
                feedback_id=feedback.id,
            ), 200
        else:
            feedback = UtilityAppFeedback(
                target_type=FEEDBACK_TARGET_EXISTING,
                feature_key=feature_key,
                app_title=app_title,
                rating=rating,
                comments=comments,
                member_id=current_user.id,
                member_name=current_user.name,
                member_email=current_user.email,
                status=FEEDBACK_STATUS_ACTIVE,
            )
            db.session.add(feedback)
            db.session.commit()
            return jsonify(
                message=f"Thank you! Your rating ({rating}/10) for {app_title} has been recorded.",
                feedback_id=feedback.id,
            ), 200

    elif target_type == FEEDBACK_TARGET_NEW:
        app_title = request.form.get("app_title", "").strip()
        if not app_title:
            return jsonify(error="Please provide a title or name for the requested utility app."), 400
        if not comments:
            return jsonify(error="Please describe what this new utility app should do and its functionality."), 400

        feedback = UtilityAppFeedback(
            target_type=FEEDBACK_TARGET_NEW,
            feature_key=None,
            app_title=app_title,
            rating=None,
            comments=comments,
            member_id=current_user.id,
            member_name=current_user.name,
            member_email=current_user.email,
            status=FEEDBACK_STATUS_ACTIVE,
        )
        db.session.add(feedback)
        db.session.commit()
        return jsonify(
            message=f"Thank you! Your proposal for '{app_title}' has been submitted for review.",
            feedback_id=feedback.id,
        ), 200

    else:
        return jsonify(error="Invalid feedback target type. Must be 'existing' or 'new'."), 400
