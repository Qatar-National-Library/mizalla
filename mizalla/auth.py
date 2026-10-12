from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import login_user, logout_user
from urllib.parse import urljoin, urlparse

from .extensions import db
from .models import Member

bp = Blueprint("auth", __name__, url_prefix="/apps")


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        member = db.session.scalar(db.select(Member).filter_by(email=email))
        if member is not None and member.check_password(password):
            login_user(member)
            target = request.args.get("next")
            if target and urlparse(urljoin(request.host_url, target)).netloc == request.host:
                return redirect(target)
            return redirect(url_for("main.project_list"))
        error = "Email or password was not recognized."
    return render_template("login.html", error=error)


@bp.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))