"""Authentication and Credential Management Blueprint.

Handles:
* User login with 5-attempt account lockout mechanism
* User logout
* Self-service password reset magic link via email (30-min validity)
* First-time account password setup magic link (24-hour validity)
"""

from datetime import datetime, timezone
import hashlib
from urllib.parse import urljoin, urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .email import send_email
from .extensions import db
from .models import Member

bp = Blueprint("auth", __name__, url_prefix="/apps")

RESET_SALT = "mizalla-password-reset-salt"
RESET_MAX_AGE_SECONDS = 1800  # 30 minutes for password reset
SETUP_MAX_AGE_SECONDS = 86400  # 24 hours for first login account setup


def get_reset_serializer() -> URLSafeTimedSerializer:
    secret_key = current_app.config.get("SECRET_KEY", "mizalla-default-secret-change-in-prod")
    return URLSafeTimedSerializer(secret_key, salt=RESET_SALT)


def generate_password_token(member: Member) -> str:
    """Generate a signed, timed token and record its sha256 hash on the member."""
    s = get_reset_serializer()
    now_utc = datetime.now(timezone.utc)
    token = s.dumps({"member_id": member.id, "email": member.email, "ts": now_utc.timestamp()})
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    member.password_reset_token_hash = token_hash
    member.password_reset_sent_at = now_utc
    return token


def verify_password_token(token: str, max_age: int = RESET_MAX_AGE_SECONDS) -> Member | None:
    """Verify that token is cryptographically valid, within max_age, and matches member's stored hash."""
    s = get_reset_serializer()
    try:
        data = s.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None

    member_id = data.get("member_id")
    if not member_id:
        return None

    member = db.session.get(Member, member_id)
    if not member or not member.password_reset_token_hash:
        return None

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if token_hash != member.password_reset_token_hash:
        return None

    return member


def send_password_reset_email(member: Member, token: str, is_welcome: bool = False) -> bool:
    """Send a magic link email for resetting or initially setting the password."""
    endpoint = "auth.set_password" if (is_welcome or member.must_set_password) else "auth.reset_password"
    reset_url = url_for(endpoint, token=token, _external=True)

    if is_welcome or member.must_set_password:
        subject = "Welcome to Mizalla – Set Your Password"
        headline = "Welcome to Mizalla!"
        subtext = (
            f"Hello {member.name},<br><br>"
            "An administrator has created an account for you on Mizalla. "
            "Please click the secure button below to set your password and access your dashboard. "
            "This link will remain valid for 24 hours."
        )
        button_text = "Set Your Password"
    else:
        subject = "Reset Your Mizalla Password"
        headline = "Password Reset Request"
        subtext = (
            f"Hello {member.name},<br><br>"
            "We received a request to reset your Mizalla password. "
            "Click the secure button below to choose a new password. "
            "This link will expire in 30 minutes.<br><br>"
            "If your account was temporarily locked due to failed login attempts, "
            "setting a new password will automatically unlock your account.<br><br>"
            "If you did not request this, you can safely ignore this email."
        )
        button_text = "Reset Password"

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 2rem; border: 1px solid #e2e8f0; border-radius: 8px; background: #ffffff;">
      <div style="text-align: center; margin-bottom: 2rem;">
        <h2 style="color: #103e30; margin: 0;">Mizalla</h2>
        <p style="color: #64748b; font-size: 0.9rem; margin-top: 0.25rem;">Qatar National Library Internal Services</p>
      </div>
      <h3 style="color: #0f172a; margin-top: 0;">{headline}</h3>
      <p style="color: #334155; line-height: 1.6; font-size: 0.95rem;">{subtext}</p>
      <div style="text-align: center; margin: 2rem 0;">
        <a href="{reset_url}" style="background-color: #103e30; color: #ffffff; padding: 0.8rem 1.8rem; text-decoration: none; border-radius: 6px; font-weight: 600; display: inline-block;">{button_text}</a>
      </div>
      <p style="color: #64748b; font-size: 0.85rem; word-break: break-all;">
        Or copy and paste this link into your browser:<br>
        <a href="{reset_url}" style="color: #103e30;">{reset_url}</a>
      </p>
      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 2rem 0;">
      <p style="color: #94a3b8; font-size: 0.8rem; text-align: center; margin: 0;">
        This is an automated security notice from Mizalla. Please do not reply directly to this email.
      </p>
    </div>
    """
    text_body = f"{headline}\n\n{member.name},\n\nPlease use the link below:\n{reset_url}\n"
    return send_email(member.email, subject, html_body, text_body)


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        member = db.session.scalar(db.select(Member).filter_by(email=email))

        if member is None:
            error = "Email or password was not recognized."
        elif member.is_locked:
            reason = member.lock_reason or "Account locked due to consecutive failed login attempts."
            error = (
                f"Your account is locked. Reason: {reason} "
                "Please use 'Forgot password?' below to unlock and reset your credentials via email, "
                "or contact an administrator."
            )
        elif member.must_set_password or not member.password_hash:
            error = (
                "Your account has been created, but your password has not been set yet. "
                "Please check your email for your invitation link, or click 'Forgot password?' below "
                "to receive a password setup link."
            )
        elif member.check_password(password):
            member.record_successful_login()
            db.session.commit()
            login_user(member)
            target = request.args.get("next")
            if target and urlparse(urljoin(request.host_url, target)).netloc == request.host:
                return redirect(target)
            return redirect(url_for("main.project_list"))
        else:
            newly_locked = member.record_failed_login(max_attempts=5)
            db.session.commit()
            if newly_locked:
                error = (
                    "Your account has been locked due to 5 consecutive failed login attempts. "
                    "Please use 'Forgot password?' below to unlock and reset your credentials, "
                    "or contact an administrator."
                )
            else:
                remaining = max(0, 5 - (member.failed_login_attempts or 0))
                error = f"Email or password was not recognized. {remaining} attempt(s) remaining before account lockout."

    return render_template("login.html", error=error)


@bp.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    notice = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if email:
            member = db.session.scalar(db.select(Member).filter_by(email=email))
            if member:
                token = generate_password_token(member)
                db.session.commit()
                send_password_reset_email(member, token, is_welcome=member.must_set_password)

        notice = "If that email address exists in our system, we have sent instructions to reset your password. Please check your inbox and spam folder."

    return render_template("forgot_password.html", notice=notice)


@bp.route("/reset-password/<token>", methods=["GET", "POST"], endpoint="reset_password")
@bp.route("/set-password/<token>", methods=["GET", "POST"], endpoint="set_password")
def reset_password(token: str):
    # Verify with 24 hours max age for initial setup
    member = verify_password_token(token, max_age=SETUP_MAX_AGE_SECONDS)
    if not member:
        flash("This password link is invalid or has expired. Please request a new link.", "error")
        return redirect(url_for("auth.forgot_password"))

    # For existing members resetting password, enforce 30 min expiration
    if not member.must_set_password:
        member = verify_password_token(token, max_age=RESET_MAX_AGE_SECONDS)
        if not member:
            flash("This password reset link has expired (links are valid for 30 minutes). Please request a new one.", "error")
            return redirect(url_for("auth.forgot_password"))

    error = None
    is_first_setup = member.must_set_password

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if len(password) < 12:
            error = "Password must be at least 12 characters long."
        elif password != confirm:
            error = "Passwords do not match. Please re-enter both fields."
        else:
            member.set_password(password)
            member.must_set_password = False
            member.unlock()
            member.password_reset_token_hash = None
            member.password_reset_sent_at = None
            db.session.commit()

            flash("Your password has been successfully saved. You can now sign in with your new credentials.", "success")
            return redirect(url_for("auth.login"))

    return render_template(
        "reset_password.html",
        token=token,
        member=member,
        is_first_setup=is_first_setup,
        error=error,
    )