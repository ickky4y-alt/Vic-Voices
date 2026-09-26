from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import DateTime
from sqlalchemy.sql import func


db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default="user", nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = db.Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_login_at = db.Column(DateTime(timezone=True), nullable=True)

    @property
    def is_active(self):
        return self.active


class VoiceModel(db.Model):
    __tablename__ = "voice_models"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)
    description = db.Column(db.Text, default="")
    category = db.Column(db.String(100), default="general")
    language = db.Column(db.String(100), default="en")
    source = db.Column(db.String(255), default="local")
    license = db.Column(db.String(255), default="unknown")
    attribution = db.Column(db.String(255), default="")
    path = db.Column(db.String(500), nullable=False)
    index_path = db.Column(db.String(500), default="")
    featured = db.Column(db.Boolean, default=False)
    enabled = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(50), default="active")
    visibility = db.Column(db.String(30), default="private", nullable=False)
    validation_status = db.Column(db.String(40), default="unvalidated", nullable=False)
    validation_error = db.Column(db.Text, default="")
    last_validated_at = db.Column(DateTime(timezone=True), nullable=True)
    file_size = db.Column(db.BigInteger, default=0)
    created_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = db.Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    imported_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    importer = db.relationship("User", foreign_keys=[imported_by])


class Conversion(db.Model):
    __tablename__ = "conversions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    model_id = db.Column(db.Integer, db.ForeignKey("voice_models.id"), nullable=True)
    voice_name = db.Column(db.String(150), nullable=False)
    status = db.Column(db.String(50), default="queued")
    input_path = db.Column(db.String(500), nullable=False)
    output_path = db.Column(db.String(500), nullable=True)
    model_name = db.Column(db.String(150), nullable=True)
    error = db.Column(db.Text, default="")
    created_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = db.Column(DateTime(timezone=True), nullable=True)
    completed_at = db.Column(DateTime(timezone=True), nullable=True)
    processing_duration_seconds = db.Column(db.Float, default=0.0)
    settings_json = db.Column(db.Text, default="{}")
    original_filename = db.Column(db.String(255), default="")
    input_duration_seconds = db.Column(db.Float, default=0.0)
    output_format = db.Column(db.String(20), default="wav")
    retention_expires_at = db.Column(DateTime(timezone=True), nullable=True, index=True)
    storage_tier = db.Column(db.String(20), default="free", nullable=False)
    output_expired = db.Column(db.Boolean, default=False, nullable=False)

    user = db.relationship("User", backref=db.backref("conversions", lazy=True))
    voice_model = db.relationship("VoiceModel")


class GeneratedAudio(db.Model):
    __tablename__ = "generated_audio"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    filename = db.Column(db.String(80), unique=True, nullable=False)
    voice_name = db.Column(db.String(150), nullable=False)
    style_name = db.Column(db.String(50), nullable=False, default="neutral")
    created_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    expires_at = db.Column(DateTime(timezone=True), nullable=False, index=True)
    storage_tier = db.Column(db.String(20), default="free", nullable=False)
    expired = db.Column(db.Boolean, default=False, nullable=False)

    user = db.relationship("User", backref=db.backref("generated_audio", lazy=True))


class Favorite(db.Model):
    __tablename__ = "favorites"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    voice_name = db.Column(db.String(150), nullable=False)
    voice_model_id = db.Column(db.Integer, db.ForeignKey("voice_models.id"), nullable=True)

    user = db.relationship("User", backref=db.backref("favorites", lazy=True))
    voice_model = db.relationship("VoiceModel")

    __table_args__ = (db.UniqueConstraint("user_id", "voice_name", name="uq_favorite_user_voice"),)


class UserActivity(db.Model):
    __tablename__ = "user_activities"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    activity_type = db.Column(db.String(100), nullable=False, index=True)
    resource_type = db.Column(db.String(80), default="")
    resource_id = db.Column(db.String(120), default="")
    description = db.Column(db.String(255), nullable=False)
    metadata_json = db.Column(db.Text, default="{}")
    created_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    user = db.relationship("User", backref=db.backref("activities", lazy=True, cascade="all, delete-orphan"))


class ContactMessage(db.Model):
    __tablename__ = "contact_messages"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(180), nullable=False)
    category = db.Column(db.String(40), default="website", nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default="open", nullable=False)
    admin_response = db.Column(db.Text, default="")
    responded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    responded_at = db.Column(DateTime(timezone=True), nullable=True)
    created_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = db.relationship("User", foreign_keys=[user_id], backref="contact_messages")
    responder = db.relationship("User", foreign_keys=[responded_by])


class UserSetting(db.Model):
    __tablename__ = "user_settings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)
    default_output_format = db.Column(db.String(20), default="wav")
    default_voice_name = db.Column(db.String(150), default="")
    reduced_motion = db.Column(db.Boolean, default=False)
    activity_enabled = db.Column(db.Boolean, default=True)

    user = db.relationship("User", backref=db.backref("settings", uselist=False, cascade="all, delete-orphan"))


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(120), nullable=False)
    target_type = db.Column(db.String(80), default="system")
    target_id = db.Column(db.String(120), default="")
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    details = db.Column(db.Text, default="")
    status = db.Column(db.String(40), default="success")
    created_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = db.relationship("User", backref=db.backref("audit_logs", lazy=True))


class AppSetting(db.Model):
    __tablename__ = "app_settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True, nullable=False)
    value = db.Column(db.Text, default="")
    updated_at = db.Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class UserEntitlement(db.Model):
    __tablename__ = "user_entitlements"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    tts_trials_remaining = db.Column(db.Integer, default=8, nullable=False)
    conversion_trials_remaining = db.Column(db.Integer, default=5, nullable=False)
    trial_started_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    trial_expires_at = db.Column(DateTime(timezone=True), nullable=False)
    access_started_at = db.Column(DateTime(timezone=True), nullable=True)
    access_expires_at = db.Column(DateTime(timezone=True), nullable=True)
    payment_status = db.Column(db.String(30), default="none", nullable=False)
    user = db.relationship("User", backref=db.backref("entitlement", uselist=False, cascade="all, delete-orphan"))


class DeviceTrial(db.Model):
    __tablename__ = "device_trials"

    id = db.Column(db.Integer, primary_key=True)
    fingerprint = db.Column(db.String(128), unique=True, nullable=False, index=True)
    guest_tts_used = db.Column(db.Integer, default=0, nullable=False)
    first_seen_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    linked_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)


class PaymentRequest(db.Model):
    __tablename__ = "payment_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    plan_key = db.Column(db.String(30), default="monthly", nullable=False)
    amount = db.Column(db.Float, default=0, nullable=False)
    currency = db.Column(db.String(12), default="USD", nullable=False)
    reference = db.Column(db.String(255), nullable=False)
    reference_code = db.Column(db.String(40), unique=True, nullable=False)
    sender_name = db.Column(db.String(160), default="")
    notes = db.Column(db.Text, default="")
    receipt_filename = db.Column(db.String(255), default="")
    status = db.Column(db.String(30), default="pending", nullable=False)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reviewed_at = db.Column(DateTime(timezone=True), nullable=True)
    verification_deadline = db.Column(DateTime(timezone=True), nullable=True)
    access_granted_at = db.Column(DateTime(timezone=True), nullable=True)
    access_expires_at = db.Column(DateTime(timezone=True), nullable=True)
    created_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    user = db.relationship("User", foreign_keys=[user_id], backref="payment_requests")


class PaymentComplaint(db.Model):
    __tablename__ = "payment_complaints"

    id = db.Column(db.Integer, primary_key=True)
    payment_request_id = db.Column(db.Integer, db.ForeignKey("payment_requests.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    message = db.Column(db.Text, nullable=False)
    attachment_filename = db.Column(db.String(255), default="")
    status = db.Column(db.String(30), default="open", nullable=False)
    admin_response = db.Column(db.Text, default="")
    next_allowed_submission_at = db.Column(DateTime(timezone=True), nullable=True)
    resolved_at = db.Column(DateTime(timezone=True), nullable=True)
    resolved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = db.Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    payment_request = db.relationship("PaymentRequest", backref=db.backref("complaints", lazy=True))
    user = db.relationship("User", foreign_keys=[user_id], backref="payment_complaints")


class JobQueueItem(db.Model):
    __tablename__ = "job_queue_items"

    id = db.Column(db.Integer, primary_key=True)
    conversion_id = db.Column(db.Integer, db.ForeignKey("conversions.id"), nullable=True)
    status = db.Column(db.String(50), default="queued")
    created_at = db.Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = db.Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    conversion = db.relationship("Conversion")
