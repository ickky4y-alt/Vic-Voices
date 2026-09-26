import os
import time
from threading import Lock
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request
from flask_login import LoginManager
from sqlalchemy import inspect, text
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash

from .admin_routes import admin_bp
from .config import config
from .models import AppSetting, AuditLog, User, db
from .models import ContactMessage, GeneratedAudio, PaymentComplaint, PaymentRequest, UserEntitlement, VoiceModel
from .audio_retention import cleanup_expired_audio, initialize_legacy_conversion_retention
from .services import ModelManager
from .user_routes import user_bp

login_manager = LoginManager()
login_manager.login_view = "user.login"
_retention_cleanup_lock = Lock()
_last_retention_cleanup = 0.0


def utc_now():
    return datetime.now(timezone.utc)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def ensure_default_admin_settings():
    defaults = {
        "registration_enabled": "true",
        "maintenance_mode": "false",
        "max_upload_mb": "50",
        "max_concurrent_jobs": "2",
        "job_timeout_seconds": "600",
        "retention_days": "7",
        "default_output_format": "wav",
        "allowed_audio_formats": "wav,mp3,flac,m4a",
        "guest_tts_trials": "3",
        "registered_tts_trials": "8",
        "conversion_trials": "5",
        "payment_account_name": "",
        "payment_account_number": "",
        "payment_bank_name": "",
        "payment_instructions": "Contact the administrator for payment instructions.",
        "billing_currency": "USD",
        "monthly_original_price": "20",
        "monthly_sale_price": "10",
        "quarterly_original_price": "50",
        "quarterly_sale_price": "30",
        "annual_original_price": "120",
        "annual_sale_price": "70",
    }
    with db.session.begin():
        for key, value in defaults.items():
            existing = AppSetting.query.filter_by(key=key).first()
            if existing is None:
                db.session.add(AppSetting(key=key, value=value))


def ensure_default_admin_user(app):
    admin_email = app.config.get("ADMIN_EMAIL") or os.environ.get("ADMIN_EMAIL") or "admin1@gmail.com"
    admin_password = app.config.get("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD") or "AdminPass123!"

    try:
        admin = User.query.filter_by(email=admin_email.lower()).first()
        if admin is None:
            admin = User(
                name="Administrator",
                email=admin_email.lower(),
                password_hash=generate_password_hash(admin_password),
                role="admin",
                is_admin=True,
                active=True,
            )
            db.session.add(admin)
            db.session.commit()
            db.session.add(AuditLog(action="admin_created", target_type="user", target_id=str(admin.id), user_id=admin.id, details="Initial admin created via default bootstrap", status="success"))
            db.session.commit()
        elif not admin.is_admin:
            admin.role = "admin"
            admin.is_admin = True
            admin.active = True
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Admin bootstrap warning: {e}")


def ensure_local_voice_models():
    try:
        manager = ModelManager()
        for item in manager.discover():
            model = VoiceModel.query.filter_by(path=item["path"]).first()
            if model is None:
                model = VoiceModel(name=item["name"], path=item["path"])
                db.session.add(model)
            model.name = item["name"]
            model.path = item["path"]
            model.index_path = item["index_path"]
            model.source = "local"
            model.enabled = True
            model.status = "active"
            model.visibility = "public"
            model.validation_status = "valid"
            model.validation_error = ""
            if Path(item["path"]).is_file():
                model.file_size = Path(item["path"]).stat().st_size
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Local voice models discovery warning: {e}")


def ensure_model_schema():
    additions = {
        "voice_models": {
            "visibility": "VARCHAR(30) DEFAULT 'private'",
            "validation_status": "VARCHAR(40) DEFAULT 'unvalidated'",
            "validation_error": "TEXT",
            "last_validated_at": "TIMESTAMP NULL",
        },
        "conversions": {
            "model_id": "INTEGER",
            "retention_expires_at": "TIMESTAMP NULL",
            "storage_tier": "VARCHAR(20) DEFAULT 'free'",
            "output_expired": "BOOLEAN DEFAULT FALSE",
        },
        "payment_requests": {
            "plan_key": "VARCHAR(30) DEFAULT 'monthly'",
            "amount": "FLOAT DEFAULT 0",
            "currency": "VARCHAR(12) DEFAULT 'USD'",
            "reference_code": "VARCHAR(40) DEFAULT ''",
            "sender_name": "VARCHAR(160) DEFAULT ''",
            "receipt_filename": "VARCHAR(255) DEFAULT ''",
            "verification_deadline": "TIMESTAMP NULL",
            "access_granted_at": "TIMESTAMP NULL",
            "access_expires_at": "TIMESTAMP NULL",
        },
    }
    try:
        inspector = inspect(db.engine)
        for table_name, columns in additions.items():
            if inspector.has_table(table_name):
                existing = {column["name"] for column in inspector.get_columns(table_name)}
                for column_name, definition in columns.items():
                    if column_name not in existing:
                        try:
                            db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"))
                            db.session.commit()
                        except Exception as col_err:
                            db.session.rollback()
                            print(f"Skipping alter column {table_name}.{column_name}: {col_err}")
    except Exception as e:
        db.session.rollback()
        print(f"Schema inspection warning: {e}")


def ensure_payment_reference_codes():
    try:
        rows = PaymentRequest.query.filter((PaymentRequest.reference_code == "") | (PaymentRequest.reference_code.is_(None))).all()
        for payment in rows:
            payment.reference_code = f"VF-LEGACY-{payment.id}"
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Payment reference code warning: {e}")


def get_app_settings_map():
    return {item.key: item.value for item in AppSetting.query.all()}


def create_app(test_config=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    if test_config and test_config.get("TESTING"):
        env_name = "testing"
    else:
        configured_env = os.environ.get("APP_ENV", "").strip().lower()
        railway_env = bool(os.environ.get("RAILWAY_ENVIRONMENT"))
        env_name = configured_env or ("production" if railway_env else "development")
        if env_name == "live":
            env_name = "production"
        elif env_name == "test":
            env_name = "testing"
        if env_name not in config:
            raise RuntimeError(f"Unsupported APP_ENV: {env_name}")
    app.config.from_object(config[env_name])
    if test_config:
        app.config.update(test_config)

    if env_name == "production":
        secret_key = os.environ.get("SECRET_KEY") or app.config.get("SECRET_KEY") or ""
        if len(secret_key) < 32:
            raise RuntimeError("Production requires a SECRET_KEY of at least 32 characters.")
        if not os.environ.get("ADMIN_EMAIL") or not os.environ.get("ADMIN_PASSWORD"):
            raise RuntimeError("Production requires ADMIN_EMAIL and ADMIN_PASSWORD to bootstrap the initial administrator.")
        database_url = app.config.get("SQLALCHEMY_DATABASE_URI") or ""
        if not database_url or database_url.startswith("sqlite:"):
            raise RuntimeError("Production requires a Railway MySQL or PostgreSQL DATABASE_URL; SQLite is disabled.")
        app.config["PREFERRED_URL_SCHEME"] = "https"

    db.init_app(app)
    login_manager.init_app(app)
    config[env_name].init_app(app)
    if env_name == "production":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    with app.app_context():
        try:
            db.create_all()
            ensure_model_schema()
            ensure_payment_reference_codes()
            ensure_default_admin_settings()
            ensure_default_admin_user(app)
            ensure_local_voice_models()
            initialize_legacy_conversion_retention()
            cleanup_expired_audio(app)
        except Exception as startup_err:
            db.session.rollback()
            print(f"App context startup initialization warning: {startup_err}")

    app.register_blueprint(user_bp)
    app.register_blueprint(admin_bp)

    @app.before_request
    def cleanup_expired_audio_periodically():
        global _last_retention_cleanup
        now = time.monotonic()
        if now - _last_retention_cleanup < 15 * 60:
            return None
        if _retention_cleanup_lock.acquire(blocking=False):
            try:
                cleanup_expired_audio(app)
                _last_retention_cleanup = time.monotonic()
            finally:
                _retention_cleanup_lock.release()
        return None

    @app.after_request
    def prevent_private_page_caching(response):
        # Dynamic pages may contain account data. Do not let the browser reuse
        # them after logout; static assets remain cacheable.
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.context_processor
    def template_context():
        settings = get_app_settings_map()
        return {
            "now": utc_now(),
            "app_settings": settings,
            "billing_currency": settings.get("billing_currency", "USD"),
            "guest_tts_trials": settings.get("guest_tts_trials", "3"),
            "monthly_plan": {"original": settings.get("monthly_original_price", "20"), "sale": settings.get("monthly_sale_price", "10")},
            "quarterly_plan": {"original": settings.get("quarterly_original_price", "50"), "sale": settings.get("quarterly_sale_price", "30")},
            "annual_plan": {"original": settings.get("annual_original_price", "120"), "sale": settings.get("annual_sale_price", "70")},
        }

    @app.route("/health")
    def health_check():
        try:
            db.session.execute(text("SELECT 1"))
        except Exception:
            db.session.rollback()
            app.logger.exception("Database health check failed")
            return jsonify({"status": "error", "service": "vic-voices", "database": "unavailable"}), 503
        return jsonify({"status": "ok", "service": "vic-voices", "database": "ok"})

    @app.errorhandler(403)
    def forbidden(_error):
        return {"error": "forbidden"}, 403

    @app.errorhandler(404)
    def not_found(_error):
        return {"error": "not found"}, 404

    return app
