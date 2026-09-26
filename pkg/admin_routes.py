import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .models import AppSetting, AuditLog, ContactMessage, Conversion, PaymentComplaint, PaymentRequest, User, UserEntitlement, VoiceModel, db
from .access_control import get_entitlement
from .audio_retention import extend_existing_audio_storage
from .services import FFmpegService, ModelManager, RVCService

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def utc_now():
    return datetime.now(timezone.utc)


def access_is_active(entitlement):
    expires_at = entitlement.access_expires_at if entitlement else None
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return bool(expires_at and expires_at > utc_now())


def record_audit(action, target_type="system", target_id="", user=None, details="", status="success"):
    actor = user or current_user
    log = AuditLog(
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        user_id=actor.id if actor and getattr(actor, "id", None) is not None else None,
        details=details,
        status=status,
    )
    db.session.add(log)
    db.session.commit()


@admin_bp.before_request
def check_admin_access():
    if request.endpoint in {"admin.login", "admin.logout"}:
        return None
    if not current_user.is_authenticated:
        return redirect(url_for("admin.login"))
    if not getattr(current_user, "is_admin", False) or not getattr(current_user, "active", True):
        abort(403)


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated and getattr(current_user, "is_admin", False):
        return redirect(url_for("admin.dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.is_admin and user.active and check_password_hash(user.password_hash, password):
            session.clear()
            login_user(user, remember=False)
            user.last_login_at = utc_now()
            db.session.commit()
            record_audit("admin_login", "user", user.id, user=user, details="Administrator login succeeded", status="success")
            flash("Logged in successfully.", "success")
            return redirect(url_for("admin.dashboard"))
        flash("Invalid administrator credentials.", "error")
    return render_template("admin/login.html")


@admin_bp.route("/logout")
def logout():
    if current_user.is_authenticated:
        record_audit("admin_logout", "user", current_user.id, user=current_user, details="Administrator logged out", status="success")
    logout_user()
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("admin.login"))


@admin_bp.route("/")
def dashboard():
    total_users = User.query.count()
    new_users = User.query.filter(User.created_at >= utc_now() - timedelta(days=7)).count()
    active_users = User.query.filter(User.last_login_at >= utc_now() - timedelta(days=30)).count()
    total_conversions = Conversion.query.count()
    successful_conversions = Conversion.query.filter_by(status="completed").count()
    failed_conversions = Conversion.query.filter_by(status="failed").count()
    processing_jobs = Conversion.query.filter_by(status="processing").count()
    queued_jobs = Conversion.query.filter_by(status="queued").count()
    total_models = VoiceModel.query.count()
    disabled_models = VoiceModel.query.filter_by(enabled=False).count()
    recent_activity = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(8).all()
    return render_template(
        "admin/dashboard.html",
        total_users=total_users,
        new_users=new_users,
        active_users=active_users,
        total_conversions=total_conversions,
        successful_conversions=successful_conversions,
        failed_conversions=failed_conversions,
        processing_jobs=processing_jobs,
        queued_jobs=queued_jobs,
        total_models=total_models,
        disabled_models=disabled_models,
        recent_activity=recent_activity,
    )


@admin_bp.route("/users")
def users():
    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", "", type=str)
    role = request.args.get("role", "")
    sort = request.args.get("sort", "created_at")
    query = User.query

    if q:
        query = query.filter(User.name.ilike(f"%{q}%") | User.email.ilike(f"%{q}%"))
    if role:
        query = query.filter_by(role=role)
    if sort == "recent":
        query = query.order_by(User.created_at.desc())
    else:
        query = query.order_by(getattr(User, sort).desc())

    pagination = query.paginate(page=page, per_page=15, error_out=False)
    for item in pagination.items:
        if item.entitlement is None:
            get_entitlement(item)
    db.session.commit()
    access_status = {item.id: access_is_active(item.entitlement) for item in pagination.items}
    return render_template("admin/users.html", users=pagination.items, pagination=pagination, access_status=access_status)


@admin_bp.route("/users/<int:user_id>")
def user_detail(user_id):
    user = User.query.get_or_404(user_id)
    entitlement = get_entitlement(user)
    db.session.commit()
    conversions = Conversion.query.filter_by(user_id=user.id).order_by(Conversion.created_at.desc()).limit(10).all()
    payments = PaymentRequest.query.filter_by(user_id=user.id).order_by(PaymentRequest.created_at.desc()).all()
    contact_messages = ContactMessage.query.filter_by(user_id=user.id).order_by(ContactMessage.created_at.desc()).all()
    return render_template("admin/user_detail.html", user=user, conversions=conversions, entitlement=entitlement, payments=payments, contact_messages=contact_messages)


@admin_bp.route("/payments")
def payments():
    query = PaymentRequest.query.join(User, PaymentRequest.user_id == User.id)
    search = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    plan = request.args.get("plan", "")
    if search:
        query = query.filter(User.email.ilike(f"%{search}%") | User.name.ilike(f"%{search}%") | PaymentRequest.reference_code.ilike(f"%{search}%"))
    if status:
        query = query.filter(PaymentRequest.status == status)
    if plan:
        query = query.filter(PaymentRequest.plan_key == plan)
    payment_items = query.order_by(PaymentRequest.created_at.desc()).limit(100).all()
    complaints = PaymentComplaint.query.filter_by(status="open").order_by(PaymentComplaint.created_at.asc()).limit(20).all()
    return render_template("admin/payments.html", payments=payment_items, complaints=complaints, search=search, status=status, plan=plan)


@admin_bp.route("/contact-messages", methods=["GET", "POST"])
def contact_messages():
    if request.method == "POST":
        message = ContactMessage.query.get_or_404(request.form.get("message_id", type=int))
        message.status = "resolved"
        message.admin_response = request.form.get("admin_response", "").strip()
        message.responded_by = current_user.id
        message.responded_at = utc_now()
        db.session.commit()
        record_audit("contact_message_resolved", "contact_message", message.id, user=current_user, details="Contact message resolved")
        flash("Contact message marked resolved.", "success")
        return redirect(url_for("admin.contact_messages"))
    status = request.args.get("status", "open")
    query = ContactMessage.query.order_by(ContactMessage.created_at.desc())
    if status in {"open", "resolved"}:
        query = query.filter_by(status=status)
    return render_template("admin/contact_messages.html", messages=query.limit(100).all(), status=status)


@admin_bp.route("/users/<int:user_id>/grant-access", methods=["POST"])
def grant_access(user_id):
    user = User.query.get_or_404(user_id)
    confirmed_payment = PaymentRequest.query.filter_by(user_id=user.id, status="confirmed").first()
    if confirmed_payment is None:
        flash("Confirm a payment request before granting unlimited access.", "error")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    entitlement = user.entitlement or UserEntitlement(user_id=user.id, tts_trials_remaining=0, conversion_trials_remaining=0, trial_expires_at=utc_now())
    duration = {"monthly": 30, "quarterly": 90, "annual": 365}.get(confirmed_payment.plan_key, 30)
    entitlement.access_started_at = utc_now()
    entitlement.access_expires_at = utc_now() + timedelta(days=duration)
    entitlement.payment_status = "confirmed"
    confirmed_payment.access_granted_at = entitlement.access_started_at
    confirmed_payment.access_expires_at = entitlement.access_expires_at
    db.session.add(entitlement)
    extend_existing_audio_storage(user)
    db.session.commit()
    record_audit("access_granted", "user", user.id, user=current_user, details="Granted 30 days of unlimited access")
    flash(f"Unlimited {confirmed_payment.plan_key} access granted for {duration} days.", "success")
    return redirect(url_for("admin.user_detail", user_id=user.id))


@admin_bp.route("/payments/<int:payment_id>/<action>", methods=["POST"])
def review_payment(payment_id, action):
    payment = PaymentRequest.query.get_or_404(payment_id)
    if action not in {"confirm", "reject"}:
        abort(404)
    payment.status = "confirmed" if action == "confirm" else "rejected"
    payment.reviewed_by = current_user.id
    payment.reviewed_at = utc_now()
    if action == "confirm":
        duration = {"monthly": 30, "quarterly": 90, "annual": 365}.get(payment.plan_key, 30)
        entitlement = payment.user.entitlement or UserEntitlement(user_id=payment.user_id, tts_trials_remaining=0, conversion_trials_remaining=0, trial_expires_at=utc_now())
        entitlement.access_started_at = utc_now()
        entitlement.access_expires_at = utc_now() + timedelta(days=duration)
        entitlement.payment_status = "confirmed"
        payment.access_granted_at = entitlement.access_started_at
        payment.access_expires_at = entitlement.access_expires_at
        db.session.add(entitlement)
        extend_existing_audio_storage(payment.user)
    db.session.commit()
    record_audit("payment_reviewed", "payment", payment.id, user=current_user, details=f"Payment {payment.status} for {payment.plan_key}")
    flash(f"Payment {payment.status}." + (" Access granted." if action == "confirm" else ""), "success")
    return redirect(url_for("admin.user_detail", user_id=payment.user_id))


@admin_bp.route("/payments/<int:payment_id>/receipt")
def payment_receipt(payment_id):
    payment = PaymentRequest.query.get_or_404(payment_id)
    if not payment.receipt_filename:
        abort(404)
    receipt_path = Path(current_app.config["UPLOAD_FOLDER"]) / payment.receipt_filename
    if not receipt_path.is_file():
        abort(404)
    return send_from_directory(receipt_path.parent, receipt_path.name, as_attachment=False)


@admin_bp.route("/complaints/<int:complaint_id>/attachment")
def complaint_attachment(complaint_id):
    complaint = PaymentComplaint.query.get_or_404(complaint_id)
    if not complaint.attachment_filename:
        abort(404)
    attachment_path = Path(current_app.config["UPLOAD_FOLDER"]) / complaint.attachment_filename
    if not attachment_path.is_file():
        abort(404)
    return send_from_directory(attachment_path.parent, attachment_path.name, as_attachment=False)


@admin_bp.route("/complaints/<int:complaint_id>/resolve", methods=["POST"])
def resolve_complaint(complaint_id):
    complaint = PaymentComplaint.query.get_or_404(complaint_id)
    complaint.status = "resolved"
    complaint.admin_response = request.form.get("admin_response", "").strip()
    complaint.resolved_by = current_user.id
    complaint.resolved_at = utc_now()
    db.session.commit()
    record_audit("payment_complaint_resolved", "payment_complaint", complaint.id, user=current_user, details="Payment complaint resolved")
    flash("Payment complaint resolved.", "success")
    return redirect(url_for("admin.payments"))


@admin_bp.route("/users/<int:user_id>/toggle-status", methods=["POST"])
def toggle_user_status(user_id):
    user = User.query.get_or_404(user_id)
    user.active = not user.active
    db.session.commit()
    record_audit("user_status_changed", "user", user.id, user=current_user, details=f"User status set to {'active' if user.active else 'disabled'}", status="success")
    flash("User account status updated.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/role", methods=["POST"])
def change_user_role(user_id):
    user = User.query.get_or_404(user_id)
    new_role = request.form.get("role", user.role)
    user.role = new_role
    user.is_admin = new_role == "admin"
    db.session.commit()
    record_audit("role_changed", "user", user.id, user=current_user, details=f"Role changed to {new_role}", status="success")
    flash("User role updated.", "success")
    return redirect(url_for("admin.user_detail", user_id=user.id))


@admin_bp.route("/models", methods=["GET", "POST"])
def models():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "general").strip()
        source = request.form.get("source", "local").strip()
        license_name = request.form.get("license", "unknown").strip()
        file = request.files.get("model_file")
        if not name or not file or not file.filename:
            flash("Name and model file are required.", "error")
            return render_template("admin/models.html", models=VoiceModel.query.order_by(VoiceModel.created_at.desc()).all())
        safe_name = secure_filename(file.filename)
        upload_dir = current_app.config.get("MODEL_FOLDER") or os.path.join(os.getcwd(), "storage", "models")
        Path(upload_dir).mkdir(parents=True, exist_ok=True)
        target_path = Path(upload_dir) / safe_name
        file.save(target_path)
        if Path(safe_name).suffix.lower() != ".pth":
            flash("Only .pth RVC model files are accepted.", "error")
            return redirect(url_for("admin.models"))
        model = VoiceModel(
            name=name,
            description=description,
            category=category,
            source=source,
            license=license_name,
            path=str(target_path),
            file_size=target_path.stat().st_size,
            enabled=False,
            status="unvalidated",
            visibility="private",
            validation_status="unvalidated",
            imported_by=current_user.id,
        )
        db.session.add(model)
        db.session.commit()
        record_audit("model_added", "voice_model", model.id, user=current_user, details=f"Model {name} added", status="success")
        flash("Model added successfully.", "success")
        return redirect(url_for("admin.models"))
    return render_template("admin/models.html", models=VoiceModel.query.order_by(VoiceModel.created_at.desc()).all())


@admin_bp.route("/models/scan", methods=["POST"])
def scan_models():
    manager = ModelManager()
    discovered = manager.discover()
    added = 0
    for item in discovered:
        model = VoiceModel.query.filter_by(path=item["path"]).first()
        if model is None:
            model = VoiceModel(name=item["name"], path=item["path"], index_path=item.get("index_path", ""), source="local", license="unknown", enabled=False, status="unvalidated", visibility="private", validation_status="unvalidated", file_size=Path(item["path"]).stat().st_size, imported_by=current_user.id)
            db.session.add(model)
            added += 1
        else:
            model.index_path = item.get("index_path", "")
            model.file_size = Path(item["path"]).stat().st_size
    db.session.commit()
    record_audit("models_scanned", "voice_model", "all", user=current_user, details=f"Discovered {len(discovered)} model files; added {added}")
    flash(f"Scan complete: {len(discovered)} .pth file(s) found, {added} new.", "success")
    return redirect(url_for("admin.models"))


@admin_bp.route("/models/<int:model_id>/validate", methods=["POST"])
def validate_model(model_id):
    model = VoiceModel.query.get_or_404(model_id)
    result = RVCService().validate_model(model.path)
    model.validation_status = "valid" if result["valid"] else "invalid"
    model.validation_error = result.get("error", "")
    model.last_validated_at = utc_now()
    if not result["valid"]:
        model.enabled = False
        model.status = "invalid"
        model.visibility = "private"
    else:
        model.status = "validated"
    db.session.commit()
    record_audit("model_validated", "voice_model", model.id, user=current_user, details=f"Validation status: {model.validation_status}", status="success" if result["valid"] else "failed")
    flash("Model validated successfully." if result["valid"] else f"Model validation failed: {model.validation_error}", "success" if result["valid"] else "error")
    return redirect(url_for("admin.models"))


@admin_bp.route("/models/<int:model_id>/toggle", methods=["POST"])
def toggle_model(model_id):
    model = VoiceModel.query.get_or_404(model_id)
    if not model.enabled and (model.validation_status != "valid" or not Path(model.path).is_file()):
        flash("Only an existing, RVC-validated model can be enabled.", "error")
        return redirect(url_for("admin.models"))
    model.enabled = not model.enabled
    model.status = "active" if model.enabled else "disabled"
    model.visibility = "public" if model.enabled else "private"
    db.session.commit()
    record_audit("model_toggled", "voice_model", model.id, user=current_user, details=f"Model {model.name} set to {'enabled' if model.enabled else 'disabled'}", status="success")
    flash("Model visibility updated.", "success")
    return redirect(url_for("admin.models"))


@admin_bp.route("/models/<int:model_id>/delete", methods=["POST"])
def delete_model(model_id):
    model = VoiceModel.query.get_or_404(model_id)
    db.session.delete(model)
    db.session.commit()
    record_audit("model_deleted", "voice_model", model_id, user=current_user, details=f"Model {model.name} deleted", status="success")
    flash("Model deleted.", "success")
    return redirect(url_for("admin.models"))


@admin_bp.route("/conversions")
def conversions():
    page = request.args.get("page", 1, type=int)
    status = request.args.get("status", "")
    query = Conversion.query.order_by(Conversion.created_at.desc())
    if status:
        query = query.filter_by(status=status)
    pagination = query.paginate(page=page, per_page=15, error_out=False)
    return render_template("admin/conversions.html", conversions=pagination.items, pagination=pagination)


@admin_bp.route("/conversions/<int:conversion_id>")
def conversion_detail(conversion_id):
    conversion = Conversion.query.get_or_404(conversion_id)
    return render_template("admin/conversion_detail.html", conversion=conversion)


@admin_bp.route("/analytics")
def analytics():
    registrations = []
    for days in [7, 30, 90]:
        start = utc_now() - timedelta(days=days)
        registrations.append({"period": f"Last {days} days", "count": User.query.filter(User.created_at >= start).count()})
    successful = Conversion.query.filter_by(status="completed").count()
    failed = Conversion.query.filter_by(status="failed").count()
    avg_duration = db.session.query(db.func.avg(Conversion.processing_duration_seconds)).scalar() or 0
    return render_template("admin/analytics.html", registrations=registrations, successful=successful, failed=failed, avg_duration=avg_duration)


@admin_bp.route("/system-health")
def system_health():
    rvc = RVCService().health_check()
    ffmpeg = FFmpegService(current_app.config.get("FFMPEG_PATH"), current_app.config.get("FFPROBE_PATH")).health_check()
    health = {
        "app": "Healthy",
        "database": "Healthy" if db.session.execute(db.text("SELECT 1")).scalar() else "Error",
        "rvc": "Ready" if rvc["ready"] else "Unavailable",
        "rvc_device": rvc["device"],
        "rvc_models": str(rvc["model_count"]),
        "rvc_python": "Ready" if rvc["python"] else "Unavailable",
        "rvc_imports": "Ready" if rvc["rvc_imports"] else "Unavailable",
        "ffmpeg": "Ready" if ffmpeg["ffmpeg"] else "Unavailable",
        "ffprobe": "Ready" if ffmpeg["ffprobe"] else "Unavailable",
        "upload_dir": "Healthy" if Path(current_app.config.get("UPLOAD_FOLDER")).exists() else "Warning",
        "output_dir": "Healthy" if Path(current_app.config.get("OUTPUT_FOLDER")).exists() else "Warning",
        "model_dir": "Healthy" if Path(current_app.config.get("MODEL_FOLDER")).exists() else "Warning",
    }
    return render_template("admin/system_health.html", health=health)


@admin_bp.route("/audit-logs")
def audit_logs():
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(200).all()
    return render_template("admin/audit_logs.html", logs=logs)


@admin_bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        trial_keys = ("guest_tts_trials", "registered_tts_trials", "conversion_trials")
        validated_trial_values = {}
        for key in trial_keys:
            if key not in request.form:
                continue
            raw_value = request.form.get(key, "").strip()
            if not raw_value.isdecimal() or len(raw_value) > 7 or int(raw_value) > 1_000_000:
                settings_map = {item.key: item.value for item in AppSetting.query.all()}
                settings_map.update({name: request.form.get(name, "") for name in trial_keys if name in request.form})
                flash("Free trial limits must be whole numbers between 0 and 1,000,000.", "error")
                return render_template("admin/settings.html", settings=settings_map)
            validated_trial_values[key] = str(int(raw_value))

        for key in [
            "registration_enabled",
            "maintenance_mode",
            "max_upload_mb",
            "max_concurrent_jobs",
            "job_timeout_seconds",
            "retention_days",
            "payment_account_name",
            "payment_account_number",
            "payment_bank_name",
            "payment_instructions",
            "billing_currency",
            "monthly_original_price",
            "monthly_sale_price",
            "quarterly_original_price",
            "quarterly_sale_price",
            "annual_original_price",
            "annual_sale_price",
            "guest_tts_trials",
            "registered_tts_trials",
            "conversion_trials",
        ]:
            if key not in request.form:
                continue
            value = validated_trial_values.get(key, request.form.get(key, ""))
            setting = AppSetting.query.filter_by(key=key).first()
            if setting is None:
                setting = AppSetting(key=key, value=value)
                db.session.add(setting)
            else:
                setting.value = value
        db.session.commit()
        record_audit("settings_changed", "system", "settings", user=current_user, details="Application settings updated", status="success")
        flash("Settings saved successfully.", "success")
    settings_map = {item.key: item.value for item in AppSetting.query.all()}
    return render_template("admin/settings.html", settings=settings_map)


@admin_bp.route("/profile", methods=["GET", "POST"])
def profile():
    if request.method == "POST":
        current_user.name = request.form.get("name", current_user.name).strip() or current_user.name
        if request.form.get("password"):
            current_password = request.form.get("current_password", "")
            if not check_password_hash(current_user.password_hash, current_password):
                flash("Current password is incorrect.", "error")
                return render_template("admin/profile.html", user=current_user)
            current_user.password_hash = generate_password_hash(request.form.get("password"))
        db.session.commit()
        record_audit("admin_profile_updated", "user", current_user.id, user=current_user, details="Admin profile updated", status="success")
        flash("Profile updated.", "success")
    return render_template("admin/profile.html", user=current_user)


@admin_bp.route("/health")
def health():
    return jsonify({"status": "ok", "database": "ok"})
