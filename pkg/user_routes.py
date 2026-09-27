import json
import os
import re
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy.exc import IntegrityError

from .forms import ConversionForm, LoginForm, RegistrationForm
from .models import AppSetting, ContactMessage, Conversion, Favorite, GeneratedAudio, PaymentComplaint, PaymentRequest, User, UserActivity, UserSetting, VoiceModel, db
from .access_control import access_state, consume_success, get_device_trial, get_entitlement, remaining_trials, trial_limit
from .audio_retention import as_utc, cleanup_expired_audio, retention_deadline_for_user_id, retention_for_user
from .services import AudioProcessor, ModelManager, RVCService
from .tts_processing import STYLE_CATALOG, STYLE_MAP, available_voices, generate_speech

user_bp = Blueprint("user", __name__)
ALLOWED_EXTENSIONS = {"wav", "mp3", "flac", "m4a", "ogg", "webm"}
conversion_executor = ThreadPoolExecutor(max_workers=1)
PAYMENT_PLANS = {
    "monthly": {"label": "Monthly", "days": 30, "original": "monthly_original_price", "sale": "monthly_sale_price"},
    "quarterly": {"label": "Quarterly", "days": 90, "original": "quarterly_original_price", "sale": "quarterly_sale_price"},
    "annual": {"label": "Annual", "days": 365, "original": "annual_original_price", "sale": "annual_sale_price"},
}
RECEIPT_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "pdf"}


def utc_now():
    return datetime.now(timezone.utc)


def elapsed_seconds(started_at, completed_at):
    if not started_at or not completed_at:
        return 0
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    return max(0, (completed_at - started_at).total_seconds())


def payment_settings():
    return {item.key: item.value for item in AppSetting.query.filter(AppSetting.key.in_(["payment_account_name", "payment_account_number", "payment_bank_name", "payment_instructions", "billing_currency", "monthly_original_price", "monthly_sale_price", "quarterly_original_price", "quarterly_sale_price", "annual_original_price", "annual_sale_price"])).all()}


def payment_plans(settings):
    return {key: {**plan, "original_price": settings.get(plan["original"], "0"), "sale_price": settings.get(plan["sale"], "0")} for key, plan in PAYMENT_PLANS.items()}


def new_payment_code():
    return f"VF-{utc_now():%Y%m%d}-{secrets.token_hex(3).upper()}"


def save_payment_upload(file_storage, folder, allowed_extensions):
    if not file_storage or not file_storage.filename:
        return ""
    filename = secure_filename(file_storage.filename)
    if file_storage.content_length and file_storage.content_length > current_app.config["PAYMENT_RECEIPT_MAX_BYTES"]:
        raise ValueError("That upload is too large. Payment files must be 8 MB or smaller.")
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in allowed_extensions:
        raise ValueError("Upload a JPG, PNG, WEBP, or PDF receipt.")
    target_dir = Path(current_app.config["UPLOAD_FOLDER"]) / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4()}_{filename}"
    file_storage.save(target_dir / stored_name)
    return f"{folder}/{stored_name}"


def estimate_conversion_seconds(duration, f0_method):
    if duration <= 30:
        return 0
    multiplier = 8 if f0_method == "pm" else 15
    return int(duration * multiplier) + 120


def run_conversion(app, conversion_id, user_id, original_filename, stored, output, selected_model, form_data):
    with app.app_context():
        conversion = db.session.get(Conversion, conversion_id)
        if conversion is None or conversion.status == "cancelled":
            return
        conversion.status = "processing"
        db.session.commit()
        try:
            RVCService().convert(str(stored), selected_model["name"], str(output), pitch=form_data["pitch"], f0_method=form_data["f0_method"], index_rate=form_data["index_rate"], protect=form_data["protect"], model_path=selected_model.get("path"), index_path=selected_model.get("index_path") or None, output_format=form_data["output_format"])
            if not Path(output).is_file() or Path(output).stat().st_size <= 0:
                raise RuntimeError("RVC conversion did not produce an audio file.")
            db.session.refresh(conversion)
            if conversion.status == "cancelled":
                return
            conversion.status = "completed"
            conversion.completed_at = utc_now()
            conversion.processing_duration_seconds = elapsed_seconds(conversion.started_at, conversion.completed_at)
            conversion.retention_expires_at, conversion.storage_tier = retention_deadline_for_user_id(user_id)
            db.session.commit()
            try:
                consume_success("conversion", user_id=user_id)
            except Exception:
                app.logger.exception("Could not consume conversion entitlement for user %s", user_id)
        except Exception:
            app.logger.exception("RVC conversion failed for user %s", user_id)
            db.session.refresh(conversion)
            if conversion.status == "cancelled":
                return
            conversion.status = "failed"
            conversion.error = "The voice conversion could not be completed. Try another voice or file."
            Path(output).unlink(missing_ok=True)
            conversion.completed_at = utc_now()
        db.session.commit()


def log_activity(activity_type, description, resource_type="", resource_id="", metadata=None):
    if not current_user.is_authenticated:
        return
    if current_user.settings and not current_user.settings.activity_enabled:
        return
    db.session.add(UserActivity(user_id=current_user.id, activity_type=activity_type, description=description, resource_type=resource_type, resource_id=str(resource_id), metadata_json=json.dumps(metadata or {})))


def owned_conversion(conversion_id):
    conversion = Conversion.query.filter_by(id=conversion_id, user_id=current_user.id).first()
    if conversion is None:
        abort(404)
    return conversion


def available_models():
    database_models = VoiceModel.query.filter_by(enabled=True, status="active", visibility="public", validation_status="valid").order_by(VoiceModel.name.asc()).all()
    if database_models:
        return [{"id": item.id, "name": item.name, "path": item.path, "index_path": item.index_path, "source": item.source, "featured": item.featured} for item in database_models]
    return []


def safe_upload(file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError("Please choose an audio file.")
    filename = secure_filename(file_storage.filename)
    if not filename or "." not in filename or filename.rsplit(".", 1)[1].lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("This audio format is not supported.")
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"{uuid.uuid4()}_{filename}"
    file_storage.save(target)
    return target, filename


@user_bp.before_request
def reject_inactive_users():
    if current_user.is_authenticated and not current_user.active:
        logout_user()
        session.clear()
        flash("Your account is disabled. Contact support if you need help.", "error")
        return redirect(url_for("user.login"))
    return None


@user_bp.route("/")
def index():
    account_entitlement = None
    if current_user.is_authenticated:
        account_entitlement = get_entitlement(current_user)
        db.session.commit()
    return render_template(
        "user/index.html",
        guest_tts_trials=trial_limit("guest_tts"),
        registered_tts_trials=trial_limit("tts"),
        conversion_trials=trial_limit("conversion"),
        preview_voices=available_voices()[:3],
        preview_styles=STYLE_CATALOG[:3],
        account_entitlement=account_entitlement,
        tts_trials_available=remaining_trials(current_user, "tts") if current_user.is_authenticated else None,
        conversion_trials_available=remaining_trials(current_user, "conversion") if current_user.is_authenticated else None,
    )


@user_bp.route("/api/voices")
def tts_voices():
    return jsonify({"voices": available_voices(), "styles": STYLE_CATALOG})


@user_bp.route("/api/tts", methods=["POST"])
def text_to_speech():
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "")
    voice = payload.get("voice", "")
    style = payload.get("style", "neutral")
    speed = payload.get("speed")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "Text cannot be empty."}), 400
    if len(text) > 2000:
        return jsonify({"error": "Text must be 2000 characters or fewer."}), 400
    allowed, reason, _ = access_state("tts")
    if not allowed:
        return jsonify({"error": reason}), 403
    output_name = f"{uuid.uuid4()}.mp3"
    output_path = Path(current_app.config["OUTPUT_FOLDER"]) / output_name
    try:
        generate_speech(text, voice, str(output_path), current_app.config.get("FFMPEG_PATH"), style, speed)
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError("Speech generation did not produce an audio file.")
        consume_success("tts")
        expires_at, storage_tier = retention_for_user(
            current_user._get_current_object() if current_user.is_authenticated else None
        )
        db.session.add(
            GeneratedAudio(
                user_id=current_user.id if current_user.is_authenticated else None,
                filename=output_name,
                voice_name=next((item["name"] for item in available_voices() if item["id"] == voice), voice),
                style_name=style,
                expires_at=expires_at,
                storage_tier=storage_tier,
            )
        )
        db.session.commit()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        current_app.logger.exception("TTS generation failed")
        return jsonify({"error": str(exc)}), 503
    return jsonify({"audio_url": url_for("user.tts_download", filename=output_name)})


@user_bp.route("/api/tts/preview", methods=["POST"])
def preview_speech():
    payload = request.get_json(silent=True) or {}
    voice = payload.get("voice", "")
    style = payload.get("style", "neutral")
    preview_style = next((item for item in STYLE_CATALOG if item["id"] == style), None)
    if preview_style is None or voice not in {item["id"] for item in available_voices()}:
        return jsonify({"error": "That voice preview is unavailable."}), 400

    output_name = f"{uuid.uuid4()}.mp3"
    output_path = Path(current_app.config["TEMP_FOLDER"]) / f"preview-{output_name}"
    try:
        generate_speech(
            preview_style["preview"],
            voice,
            str(output_path),
            current_app.config.get("FFMPEG_PATH"),
            style,
        )
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError("The voice preview did not produce an audio file.")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        current_app.logger.exception("Free TTS preview failed")
        return jsonify({"error": str(exc)}), 503
    return jsonify({"audio_url": url_for("user.tts_preview_download", filename=output_path.name)})


@user_bp.route("/preview-audio/<path:filename>")
def tts_preview_download(filename):
    if not re.fullmatch(r"preview-[0-9a-fA-F-]{36}\.mp3", filename):
        abort(404)
    preview_path = Path(current_app.config["TEMP_FOLDER"]) / filename
    if not preview_path.is_file() or preview_path.stat().st_mtime < (utc_now().timestamp() - 24 * 60 * 60):
        preview_path.unlink(missing_ok=True)
        abort(404)
    return send_from_directory(preview_path.parent, preview_path.name, as_attachment=False)


@user_bp.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    next_url = request.args.get("next") or request.form.get("next")
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.strip().lower()).first()
        if user and not user.active:
            flash("This account is currently disabled.", "error")
        elif user and check_password_hash(user.password_hash, form.password.data):
            session.clear()
            login_user(user, remember=False)
            user.last_login_at = utc_now()
            log_activity("login", "Signed in to Vic Voices")
            db.session.commit()
            flash("Logged in successfully.", "success")
            return redirect(next_url if next_url and next_url.startswith("/") else url_for("user.dashboard"))
        else:
            flash("Invalid email or password.", "error")
    return render_template("user/login.html", form=form, next_url=next_url)


@user_bp.route("/register", methods=["GET", "POST"])
def register():
    form = RegistrationForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "error")
        else:
            user = User(name=form.name.data.strip(), email=email, password_hash=generate_password_hash(form.password.data), active=True)
            db.session.add(user)
            db.session.flush()
            db.session.add(UserSetting(user_id=user.id))
            entitlement = get_entitlement(user)
            device_trial = get_device_trial()
            device_trial.linked_user_id = user.id
            db.session.add(UserActivity(user_id=user.id, activity_type="account_created", description="Account created"))
            db.session.commit()
            session.clear()
            login_user(user)
            flash("Account created successfully.", "success")
            return redirect(url_for("user.dashboard"))
    return render_template("user/register.html", form=form)


@user_bp.route("/logout")
def logout():
    if current_user.is_authenticated:
        log_activity("logout", "Signed out of Vic Voices")
        db.session.commit()
    logout_user()
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("user.index"))


@user_bp.route("/dashboard")
@login_required
def dashboard():
    conversion_query = Conversion.query.filter_by(user_id=current_user.id)
    recent = conversion_query.order_by(Conversion.created_at.desc()).limit(6).all()
    favorites = Favorite.query.filter_by(user_id=current_user.id).order_by(Favorite.id.desc()).limit(6).all()
    processing = conversion_query.filter(Conversion.status.in_(["queued", "processing"])).count()
    completed = conversion_query.filter_by(status="completed").count()
    failed = conversion_query.filter_by(status="failed").count()
    used_voices = db.session.query(Conversion.voice_name, db.func.count(Conversion.id)).filter_by(user_id=current_user.id).group_by(Conversion.voice_name).order_by(db.func.max(Conversion.created_at).desc()).limit(6).all()
    entitlement = get_entitlement(current_user)
    tts_trials_available = remaining_trials(current_user, "tts")
    conversion_trials_available = remaining_trials(current_user, "conversion")
    db.session.commit()
    return render_template("user/dashboard.html", total=conversion_query.count(), recent=recent, favorites=favorites, processing=processing, completed=completed, failed=failed, used_voices=used_voices, entitlement=entitlement, tts_trials_available=tts_trials_available, conversion_trials_available=conversion_trials_available)


@user_bp.route("/billing", methods=["GET", "POST"])
@login_required
def billing():
    if request.method == "POST":
        payment_id = request.form.get("payment_id", type=int)
        reference = request.form.get("reference", "").strip()
        sender_name = request.form.get("sender_name", "").strip()
        notes = request.form.get("notes", "").strip()
        payment = PaymentRequest.query.filter_by(id=payment_id, user_id=current_user.id, reference_code=reference).first()
        if payment is None or payment.status != "initiated":
            flash("Start a payment from one of the plans before submitting a receipt.", "error")
        elif not sender_name:
            flash("Sender name is required so the payment can be matched.", "error")
        else:
            try:
                receipt = save_payment_upload(request.files.get("receipt"), "payment-receipts", RECEIPT_EXTENSIONS)
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                if not receipt:
                    flash("A payment receipt is required.", "error")
                else:
                    payment.sender_name = sender_name
                    payment.reference = reference
                    payment.notes = notes
                    payment.receipt_filename = receipt
                    payment.status = "pending"
                    payment.verification_deadline = utc_now() + timedelta(minutes=30)
                    try:
                        db.session.commit()
                    except IntegrityError:
                        db.session.rollback()
                        flash("This payment session is no longer valid. Please choose the plan again and resubmit.", "error")
                        return redirect(url_for("user.billing"))
                    flash("Payment received. It is being reviewed calmly and should be verified within 30 minutes.", "success")
                    return redirect(url_for("user.billing"))
    entitlement = get_entitlement(current_user)
    settings = payment_settings()
    plans = payment_plans(settings)
    payments = PaymentRequest.query.filter_by(user_id=current_user.id).order_by(PaymentRequest.created_at.desc()).all()
    complaints = PaymentComplaint.query.filter_by(user_id=current_user.id).order_by(PaymentComplaint.created_at.desc()).all()
    complaint_payments = []
    current_time = utc_now()
    for payment in payments:
        deadline = payment.verification_deadline
        if payment.status == "pending" and deadline:
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if current_time >= deadline:
                complaint_payments.append(payment)
    access_expires_at = entitlement.access_expires_at
    tts_trials_available = remaining_trials(current_user, "tts")
    conversion_trials_available = remaining_trials(current_user, "conversion")
    if access_expires_at and access_expires_at.tzinfo is None:
        access_expires_at = access_expires_at.replace(tzinfo=timezone.utc)
    paid_access_active = bool(access_expires_at and access_expires_at > utc_now())
    return render_template("user/billing.html", entitlement=entitlement, settings=settings, plans=plans, payments=payments, complaints=complaints, complaint_payments=complaint_payments, paid_access_active=paid_access_active, tts_trials_available=tts_trials_available, conversion_trials_available=conversion_trials_available)


@user_bp.route("/billing/payment-intent", methods=["POST"])
@login_required
def payment_intent():
    settings = payment_settings()
    plans = payment_plans(settings)
    payload = request.get_json(silent=True) or {}
    plan_key = payload.get("plan") if request.is_json else request.form.get("plan")
    if plan_key not in plans:
        return jsonify({"error": "That payment plan is not available."}), 400
    plan = plans[plan_key]
    reference_code = new_payment_code()
    try:
        amount = float(plan["sale_price"])
    except (TypeError, ValueError):
        return jsonify({"error": "This plan is temporarily unavailable. Please contact support."}), 503
    payment = PaymentRequest(user_id=current_user.id, plan_key=plan_key, amount=amount, currency=settings.get("billing_currency", "USD"), reference=reference_code, reference_code=reference_code, status="initiated")
    db.session.add(payment)
    db.session.commit()
    return jsonify({"payment_id": payment.id, "reference_code": payment.reference_code, "plan": plan["label"], "amount": payment.amount, "currency": payment.currency, "bank_name": settings.get("payment_bank_name", ""), "account_name": settings.get("payment_account_name", ""), "account_number": settings.get("payment_account_number", ""), "instructions": settings.get("payment_instructions", "Complete the transfer, then upload your receipt below.")})


@user_bp.route("/billing/receipts/<int:payment_id>")
@login_required
def payment_receipt(payment_id):
    payment = PaymentRequest.query.filter_by(id=payment_id, user_id=current_user.id).first_or_404()
    if not payment.receipt_filename:
        abort(404)
    receipt_path = Path(current_app.config["UPLOAD_FOLDER"]) / payment.receipt_filename
    if not receipt_path.is_file():
        abort(404)
    return send_from_directory(receipt_path.parent, receipt_path.name, as_attachment=False)


@user_bp.route("/billing/<int:payment_id>/complaints", methods=["POST"])
@login_required
def submit_payment_complaint(payment_id):
    payment = PaymentRequest.query.filter_by(id=payment_id, user_id=current_user.id).first_or_404()
    if payment.status != "pending" or not payment.verification_deadline or utc_now() < payment.verification_deadline:
        flash("You can contact support here after the 30-minute review window if the payment is still pending.", "error")
        return redirect(url_for("user.billing"))
    latest = PaymentComplaint.query.filter_by(payment_request_id=payment.id).order_by(PaymentComplaint.created_at.desc()).first()
    if latest and latest.next_allowed_submission_at and utc_now() < latest.next_allowed_submission_at:
        flash("Please wait a few minutes before sending another complaint about this payment.", "error")
        return redirect(url_for("user.billing"))
    message = request.form.get("message", "").strip()
    if not message:
        flash("Please describe the payment issue.", "error")
        return redirect(url_for("user.billing"))
    try:
        attachment = save_payment_upload(request.files.get("attachment"), "payment-complaints", RECEIPT_EXTENSIONS)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("user.billing"))
    complaint = PaymentComplaint(payment_request_id=payment.id, user_id=current_user.id, message=message, attachment_filename=attachment, next_allowed_submission_at=utc_now() + timedelta(minutes=5))
    db.session.add(complaint)
    db.session.commit()
    flash("Your support request was sent to the payment team.", "success")
    return redirect(url_for("user.billing"))


@user_bp.route("/health")
def health():
    return jsonify({"status": "ok", "service": "vic-voices"})


@user_bp.route("/converter", methods=["GET", "POST"])
@login_required
def converter():
    form = ConversionForm()
    models = available_models()

    def render_converter_page():
        entitlement = get_entitlement(current_user)
        tts_trials_available = remaining_trials(current_user, "tts")
        conversion_trials_available = remaining_trials(current_user, "conversion")
        db.session.commit()
        tts_allowed, tts_message, _ = access_state("tts")
        conversion_allowed, conversion_message, _ = access_state("conversion")
        return render_template(
            "user/converter.html",
            form=form,
            models=models,
            entitlement=entitlement,
            tts_trials_available=tts_trials_available,
            conversion_trials_available=conversion_trials_available,
            tts_allowed=tts_allowed,
            tts_message=tts_message,
            conversion_allowed=conversion_allowed,
            conversion_message=conversion_message,
        )

    entitlement = get_entitlement(current_user)
    conversion_trials_available = remaining_trials(current_user, "conversion")
    form.model_id.choices = [
        (str(model["id"]), f"{model['name']} · {conversion_trials_available} conversion trial(s) available")
        for model in models
    ]
    requested_model_id = request.args.get("model_id", "")
    if request.method == "GET" and any(str(model["id"]) == requested_model_id for model in models):
        form.model_id.data = requested_model_id
    if form.validate_on_submit():
        allowed, reason, _ = access_state("conversion")
        if not allowed:
            flash(reason, "error")
            return render_converter_page()
        active_jobs = Conversion.query.filter(Conversion.user_id == current_user.id, Conversion.status.in_(["queued", "processing"])).count()
        daily_jobs = Conversion.query.filter(Conversion.user_id == current_user.id, Conversion.created_at >= utc_now().replace(hour=0, minute=0, second=0, microsecond=0)).count()
        if active_jobs >= current_app.config["MAX_CONCURRENT_CONVERSIONS_PER_USER"]:
            flash("You already have the maximum number of active conversions. Wait for one to finish.", "error")
            return render_converter_page()
        if daily_jobs >= current_app.config["DAILY_CONVERSION_LIMIT"]:
            flash("You have reached today's conversion limit. Try again tomorrow.", "error")
            return render_converter_page()
        selected_model = next((model for model in models if str(model["id"]) == form.model_id.data), None)
        if selected_model is None:
            flash("That voice is no longer available.", "error")
            return render_converter_page()
        uploaded_file = request.files.get("audio_file")
        recorded_path = session.get("recorded_audio_path") if not uploaded_file or not uploaded_file.filename else None
        if recorded_path:
            stored = Path(recorded_path).resolve()
            upload_root = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
            if upload_root not in stored.parents or not stored.is_file():
                session.pop("recorded_audio_path", None)
                session.pop("recorded_audio_name", None)
                flash("The recorded audio is no longer available. Please record it again.", "error")
                return render_converter_page()
            original_filename = session.get("recorded_audio_name", stored.name)
        else:
            try:
                stored, original_filename = safe_upload(uploaded_file)
            except ValueError as exc:
                flash(str(exc), "error")
                return render_converter_page()
        session.pop("recorded_audio_path", None)
        session.pop("recorded_audio_name", None)
        duration = AudioProcessor.get_duration_seconds(stored) or 0
        if duration > current_app.config["MAX_AUDIO_DURATION"]:
            stored.unlink(missing_ok=True)
            flash(f"Audio exceeds the {current_app.config['MAX_AUDIO_DURATION']} second limit.", "error")
            return render_converter_page()
        effective_f0_method = form.f0_method.data
        if duration > 30:
            effective_f0_method = os.environ.get("RVC_LONG_AUDIO_F0_METHOD", "pm")
        output_format = form.output_format.data or "wav"
        effective_index_rate = (
            form.index_rate.data if form.index_rate.data is not None else 0.0
        ) if selected_model.get("index_path") else 0.0
        output = Path(current_app.config["OUTPUT_FOLDER"]) / f"{uuid.uuid4()}.{output_format}"
        conversion = Conversion(user_id=current_user.id, model_id=selected_model["id"], voice_name=selected_model["name"], model_name=selected_model["name"], status="processing", input_path=str(stored), output_path=str(output), original_filename=original_filename, input_duration_seconds=duration, output_format=output_format, settings_json=json.dumps({"pitch": form.pitch.data or 0, "index_rate": effective_index_rate, "protect": form.protect.data or 0.33, "f0_method": effective_f0_method, "estimated_seconds": estimate_conversion_seconds(duration, effective_f0_method)}), started_at=utc_now())
        db.session.add(conversion)
        db.session.flush()
        log_activity("conversion_started", f"Conversion started for {original_filename}", "conversion", conversion.id, {"voice": selected_model["name"], "model_id": selected_model["id"]})
        db.session.commit()
        form_data = {
            "pitch": form.pitch.data or 0,
            "f0_method": effective_f0_method,
            "index_rate": effective_index_rate,
            "protect": form.protect.data or 0.33,
            "output_format": output_format,
            "estimated_seconds": estimate_conversion_seconds(duration, effective_f0_method),
        }
        conversion_executor.submit(run_conversion, current_app._get_current_object(), conversion.id, current_user.id, original_filename, stored, output, selected_model, form_data)
        return redirect(url_for("user.result", conversion_id=conversion.id))
    return render_converter_page()


@user_bp.route("/record", methods=["GET", "POST"])
@login_required
def record():
    if request.method == "POST":
        try:
            stored, original_filename = safe_upload(request.files.get("audio_file"))
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("user/record.html")
        session["recorded_audio_path"] = str(stored)
        session["recorded_audio_name"] = original_filename
        log_activity("recording_created", f"Recording created: {original_filename}")
        db.session.commit()
        return redirect(url_for("user.converter"))
    return render_template("user/record.html")


@user_bp.route("/result/<int:conversion_id>")
@login_required
def result(conversion_id):
    conversion = owned_conversion(conversion_id)
    if conversion.retention_expires_at and as_utc(conversion.retention_expires_at) <= utc_now():
        cleanup_expired_audio(current_app._get_current_object())
        db.session.refresh(conversion)
    try:
        settings = json.loads(conversion.settings_json or "{}")
    except json.JSONDecodeError:
        settings = {}
    estimated_seconds = int(settings.get("estimated_seconds") or estimate_conversion_seconds(conversion.input_duration_seconds or 0, settings.get("f0_method", "pm")))
    started_timestamp = conversion.started_at.timestamp() if conversion.started_at else 0
    return render_template("user/result.html", conversion=conversion, estimated_seconds=estimated_seconds, started_timestamp=started_timestamp)


@user_bp.route("/download/<int:conversion_id>")
@login_required
def download(conversion_id):
    conversion = owned_conversion(conversion_id)
    if conversion.retention_expires_at and as_utc(conversion.retention_expires_at) <= utc_now():
        cleanup_expired_audio(current_app._get_current_object())
        db.session.refresh(conversion)
    if conversion.status != "completed" or not conversion.output_path or not os.path.exists(conversion.output_path):
        abort(404)
    log_activity("result_downloaded", "Downloaded conversion result", "conversion", conversion.id)
    db.session.commit()
    return send_from_directory(os.path.dirname(conversion.output_path), os.path.basename(conversion.output_path), as_attachment=True)


@user_bp.route("/download/<path:filename>")
def tts_download(filename):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}\.mp3", filename):
        abort(404)
    audio = GeneratedAudio.query.filter_by(filename=filename).first_or_404()
    if audio.user_id is not None and (not current_user.is_authenticated or audio.user_id != current_user.id):
        abort(404)
    if audio.expired or as_utc(audio.expires_at) <= utc_now():
        cleanup_expired_audio(current_app._get_current_object())
        abort(404)
    output_path = Path(current_app.config["OUTPUT_FOLDER"]) / audio.filename
    if not output_path.is_file():
        audio.expired = True
        db.session.commit()
        abort(404)
    return send_from_directory(output_path.parent, output_path.name, as_attachment=True)


@user_bp.route("/conversion/<int:conversion_id>/delete", methods=["POST"])
@login_required
def delete_conversion(conversion_id):
    conversion = owned_conversion(conversion_id)
    for path in (conversion.input_path, conversion.output_path):
        if path:
            Path(path).unlink(missing_ok=True)
    log_activity("conversion_deleted", f"Deleted conversion {conversion.id}", "conversion", conversion.id)
    db.session.delete(conversion)
    db.session.commit()
    flash("Conversion deleted.", "success")
    return redirect(url_for("user.history"))


@user_bp.route("/conversion/<int:conversion_id>/cancel", methods=["POST"])
@login_required
def cancel_conversion(conversion_id):
    conversion = owned_conversion(conversion_id)
    if conversion.status not in {"queued", "processing"}:
        flash("This conversion can no longer be cancelled.", "error")
    else:
        conversion.status = "cancelled"
        log_activity("conversion_cancelled", f"Cancelled conversion {conversion.id}", "conversion", conversion.id)
        db.session.commit()
        flash("Conversion cancelled.", "success")
    return redirect(url_for("user.history_detail", conversion_id=conversion.id))


@user_bp.route("/history")
@login_required
def history():
    cleanup_expired_audio(current_app._get_current_object())
    query = Conversion.query.filter_by(user_id=current_user.id)
    status = request.args.get("status", "")
    search = request.args.get("q", "").strip()
    if status:
        query = query.filter_by(status=status)
    if search:
        query = query.filter(Conversion.original_filename.ilike(f"%{search}%") | Conversion.voice_name.ilike(f"%{search}%"))
    pagination = query.order_by(Conversion.created_at.desc()).paginate(page=request.args.get("page", 1, type=int), per_page=12, error_out=False)
    generated_audio = GeneratedAudio.query.filter_by(user_id=current_user.id).order_by(GeneratedAudio.created_at.desc()).limit(50).all()
    activities = UserActivity.query.filter_by(user_id=current_user.id).order_by(UserActivity.created_at.desc()).limit(20).all()
    return render_template("user/history.html", history=pagination.items, generated_audio=generated_audio, pagination=pagination, status=status, search=search, activities=activities)


@user_bp.route("/history/<int:conversion_id>")
@login_required
def history_detail(conversion_id):
    return render_template("user/result.html", conversion=owned_conversion(conversion_id))


@user_bp.route("/favorites")
@login_required
def favorites():
    return render_template("user/favorites.html", favorites=Favorite.query.filter_by(user_id=current_user.id).order_by(Favorite.id.desc()).all())


@user_bp.route("/favorites/toggle/<int:model_id>", methods=["POST"])
@login_required
def toggle_favorite(model_id):
    model = VoiceModel.query.filter_by(id=model_id, enabled=True, status="active", visibility="public", validation_status="valid").first_or_404()
    item = Favorite.query.filter_by(user_id=current_user.id, voice_name=model.name).first()
    if item:
        db.session.delete(item)
        log_activity("voice_unfavorited", f"Removed {model.name} from favorites", "voice_model", model.id)
        message = "Voice removed from favorites."
    else:
        db.session.add(Favorite(user_id=current_user.id, voice_name=model.name, voice_model_id=model.id))
        log_activity("voice_favorited", f"Added {model.name} to favorites", "voice_model", model.id)
        message = "Voice added to favorites."
    db.session.commit()
    flash(message, "success")
    return redirect(request.referrer or url_for("user.voice_library"))


@user_bp.route("/activity")
@login_required
def activity():
    pagination = UserActivity.query.filter_by(user_id=current_user.id).order_by(UserActivity.created_at.desc()).paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)
    return render_template("user/activity.html", activities=pagination.items, pagination=pagination)


@user_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_user.name = request.form.get("name", current_user.name).strip() or current_user.name
        new_password = request.form.get("password", "")
        if new_password:
            if not check_password_hash(current_user.password_hash, request.form.get("current_password", "")):
                flash("Current password is incorrect.", "error")
                return render_template("user/profile.html", user=current_user)
            if len(new_password) < 10 or not any(char.isupper() for char in new_password) or not any(char.isdigit() for char in new_password):
                flash("New password must be at least 10 characters with uppercase and numeric characters.", "error")
                return render_template("user/profile.html", user=current_user)
            current_user.password_hash = generate_password_hash(new_password)
            log_activity("password_changed", "Password changed")
        log_activity("profile_updated", "Profile updated")
        db.session.commit()
        flash("Profile updated.", "success")
    return render_template("user/profile.html", user=current_user)


@user_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user_settings = current_user.settings or UserSetting(user_id=current_user.id)
    if not user_settings.id:
        db.session.add(user_settings)
    if request.method == "POST":
        user_settings.default_output_format = request.form.get("default_output_format", "wav")
        user_settings.default_voice_name = request.form.get("default_voice_name", "")
        user_settings.reduced_motion = request.form.get("reduced_motion") == "on"
        user_settings.activity_enabled = request.form.get("activity_enabled") == "on"
        db.session.commit()
        flash("Settings updated.", "success")
    return render_template("user/settings.html", settings=user_settings, models=available_models())


@user_bp.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    if request.form.get("confirm") != "DELETE":
        flash("Type DELETE to confirm account removal.", "error")
        return redirect(url_for("user.profile"))
    for conversion in Conversion.query.filter_by(user_id=current_user.id).all():
        for path in (conversion.input_path, conversion.output_path):
            if path:
                Path(path).unlink(missing_ok=True)
    user = db.session.get(User, current_user.id)
    logout_user()
    session.clear()
    db.session.delete(user)
    db.session.commit()
    flash("Your account and personal files were deleted.", "success")
    return redirect(url_for("user.index"))


@user_bp.route("/voice-library")
def voice_library():
    models = available_models()
    query = request.args.get("q", "").strip().lower()
    if query:
        models = [model for model in models if query in model["name"].lower()]
    return render_template("user/voice_library.html", models=models, tts_voices=available_voices(), tts_styles=STYLE_CATALOG)


@user_bp.route("/privacy")
def privacy():
    return render_template("user/privacy.html")


@user_bp.route("/terms")
def terms():
    return render_template("user/terms.html")


@user_bp.route("/about")
def about():
    return render_template("user/about.html")


@user_bp.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        subject = request.form.get("subject", "").strip()
        category = request.form.get("category", "website")
        message = request.form.get("message", "").strip()
        if not name or not email or "@" not in email or not subject or not message:
            flash("Please complete your name, email, subject, and message.", "error")
        elif category not in {"website", "audio", "payment", "other"}:
            flash("Please choose a valid message category.", "error")
        else:
            db.session.add(ContactMessage(user_id=current_user.id if current_user.is_authenticated else None, name=name, email=email, subject=subject, category=category, message=message))
            db.session.commit()
            flash("Message sent. The Vic Voices team will get back to you soon.", "success")
            return redirect(url_for("user.contact"))
    messages = []
    if current_user.is_authenticated:
        messages = ContactMessage.query.filter_by(user_id=current_user.id).order_by(ContactMessage.created_at.desc()).all()
    return render_template("user/contact.html", messages=messages)


@user_bp.route("/help")
def help_page():
    return render_template("user/help.html")
