"""Flask application factory and web routes."""

from __future__ import annotations

from typing import Any, cast

from flask import Blueprint, Flask, flash, redirect, render_template, request, url_for
from flask_wtf import CSRFProtect

from netadmin.auth import (
    admin_required,
    authenticate_radius,
    current_identity,
    extension,
    load_identity,
    login_required,
    safe_next_url,
    verify_root,
)
from netadmin.config import Settings
from netadmin.forms import (
    DeleteDnsForm,
    DnsRecordForm,
    LoginForm,
    PasswordForm,
    RadiusUserForm,
    ReservationForm,
)
from netadmin.services.agents import AgentClient
from netadmin.services.dns import BindService
from netadmin.store import Store

web = Blueprint("web", __name__)


def _settings() -> Settings:
    return cast(Settings, extension("netadmin_settings"))


def _store() -> Store:
    return cast(Store, extension("netadmin_store"))


def _audit(action: str, target: str, success: bool = True) -> None:
    identity = current_identity()
    _store().audit(identity.username, action, target, success)


@web.route("/login", methods=["GET", "POST"])
def login() -> Any:
    """Authenticate root locally or a user through RADIUS."""
    form = LoginForm()
    if form.validate_on_submit():
        username = (form.username.data or "").strip()
        supplied_password = form.password.data or ""
        settings = _settings()
        role = (
            "admin"
            if username == "root" and verify_root(settings.root_password_hash, supplied_password)
            else authenticate_radius(username, supplied_password, settings)
        )
        if role:
            session_id = _store().create_session(username, role, settings.session_minutes * 60)
            _store().audit(username, "login", username, True)
            response = redirect(safe_next_url(request.args.get("next")))
            response.set_cookie(
                "netadmin_session",
                session_id,
                secure=True,
                httponly=True,
                samesite="Strict",
                max_age=None,
            )
            return response
        _store().audit(username, "login", username, False)
        flash("Invalid username, password, or authorization role.", "danger")
    return render_template("login.html", form=form)


@web.route("/logout", methods=["POST"])
@login_required
def logout() -> Any:
    """Invalidate the current session."""
    session_id = request.cookies.get("netadmin_session", "")
    _audit("logout", current_identity().username)
    _store().delete_session(session_id)
    response = redirect(url_for("web.login"))
    response.delete_cookie("netadmin_session")
    return response


@web.route("/", methods=["GET"])
@login_required
def dashboard() -> Any:
    """Render the role-aware landing page."""
    return render_template("dashboard.html")


@web.route("/dns", methods=["GET"])
@admin_required
def dns_index() -> Any:
    """Show records for a selected authoritative zone."""
    zone = request.args.get("zone", "")
    records = extension("netadmin_dns").records(zone) if zone else []
    return render_template(
        "dns.html",
        zone=zone,
        records=records,
        record_form=DnsRecordForm(),
        delete_form=DeleteDnsForm(),
    )


@web.route("/dns/records", methods=["POST"])
@admin_required
def dns_save() -> Any:
    """Create or replace a DNS RRset."""
    form = DnsRecordForm()
    if form.validate_on_submit():
        extension("netadmin_dns").upsert(
            (form.zone.data or "").rstrip("."),
            form.name.data or "",
            form.record_type.data or "",
            int(form.ttl.data or "300"),
            form.value.data or "",
        )
        _audit("dns.upsert", f"{form.name.data}.{form.zone.data}/{form.record_type.data}")
        flash("DNS record saved.", "success")
    else:
        flash("Invalid DNS record.", "danger")
    return redirect(url_for("web.dns_index", zone=form.zone.data))


@web.route("/dns/records/delete", methods=["POST"])
@admin_required
def dns_delete() -> Any:
    """Delete a DNS value or RRset."""
    form = DeleteDnsForm()
    if form.validate_on_submit():
        extension("netadmin_dns").delete(
            form.zone.data, form.name.data, form.record_type.data, form.value.data
        )
        _audit("dns.delete", f"{form.name.data}.{form.zone.data}/{form.record_type.data}")
        flash("DNS record deleted.", "success")
    return redirect(url_for("web.dns_index", zone=form.zone.data))


@web.route("/dhcp", methods=["GET", "POST"])
@admin_required
def dhcp_index() -> Any:
    """List leases and manage reservations through the DHCP agent."""
    client = cast(AgentClient, extension("netadmin_dhcp"))
    form = ReservationForm()
    if form.validate_on_submit():
        client.post(
            "reservations",
            {"name": form.name.data, "mac": form.mac.data, "address": form.address.data},
        )
        _audit("dhcp.reservation.upsert", form.name.data or "")
        flash("Reservation saved.", "success")
        return redirect(url_for("web.dhcp_index"))
    return render_template(
        "dhcp.html", form=form, reservations=client.get("reservations"), leases=client.get("leases")
    )


@web.route("/dhcp/reservations/delete", methods=["POST"])
@admin_required
def dhcp_delete() -> Any:
    """Delete a managed DHCP reservation."""
    name = request.form.get("name", "")
    cast(AgentClient, extension("netadmin_dhcp")).post("reservations/delete", {"name": name})
    _audit("dhcp.reservation.delete", name)
    return redirect(url_for("web.dhcp_index"))


@web.route("/radius", methods=["GET", "POST"])
@admin_required
def radius_index() -> Any:
    """Manage file-backed RADIUS users."""
    client = cast(AgentClient, extension("netadmin_radius"))
    form = RadiusUserForm()
    if form.validate_on_submit():
        client.post(
            "users",
            {
                "username": form.username.data,
                "password": form.password.data,
                "role": form.role.data,
                "disabled": form.disabled.data,
            },
        )
        _audit("radius.user.upsert", form.username.data or "")
        flash("RADIUS user saved.", "success")
        return redirect(url_for("web.radius_index"))
    return render_template("radius.html", form=form, users=client.get("users"))


@web.route("/password", methods=["GET", "POST"])
@login_required
def password() -> Any:
    """Allow a RADIUS user to change only their own password."""
    identity = current_identity()
    form = PasswordForm()
    if identity.username == "root":
        flash("The emergency root password is managed through its environment hash.", "info")
        return redirect(url_for("web.dashboard"))
    if form.validate_on_submit():
        if (
            authenticate_radius(identity.username, form.current_password.data or "", _settings())
            is None
        ):
            flash("Current password is incorrect.", "danger")
        else:
            cast(AgentClient, extension("netadmin_radius")).post(
                "users/password",
                {"username": identity.username, "password": form.new_password.data},
            )
            _audit("radius.password.change", identity.username)
            _store().delete_user_sessions(identity.username)
            response = redirect(url_for("web.login"))
            response.delete_cookie("netadmin_session")
            flash("Password changed. Sign in again.", "success")
            return response
    return render_template("password.html", form=form)


@web.route("/audit", methods=["GET"])
@admin_required
def audit() -> Any:
    """Show recent audit records."""
    return render_template("audit.html", events=_store().recent_audit())


def create_app(settings: Settings | None = None) -> Flask:
    """Create and configure NetAdmin."""
    runtime = settings or Settings.from_env()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=runtime.secret_key,
        WTF_CSRF_TIME_LIMIT=3600,
        MAX_CONTENT_LENGTH=1_048_576,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )
    CSRFProtect(app)
    store = Store(runtime.data_dir / "netadmin.sqlite3")
    app.extensions.update(
        netadmin_settings=runtime,
        netadmin_store=store,
        netadmin_dns=BindService(
            runtime.dns_server,
            runtime.dns_tsig_name,
            runtime.dns_tsig_secret,
            runtime.dns_tsig_algorithm,
        ),
        netadmin_dhcp=AgentClient(runtime.dhcp_agent_url, runtime.agent_token),
        netadmin_radius=AgentClient(runtime.radius_agent_url, runtime.agent_token),
    )
    app.before_request(load_identity)
    app.register_blueprint(web)
    return app
