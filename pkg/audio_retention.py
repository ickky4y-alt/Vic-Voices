"""Retention policy for user-generated audio outputs."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Conversion, GeneratedAudio, User, UserEntitlement, db

FREE_AUDIO_RETENTION_DAYS = 7
PAID_AUDIO_GRACE_DAYS = 7


def utc_now():
    return datetime.now(timezone.utc)


def as_utc(value):
    if value and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def paid_access_expiry(user):
    entitlement = None
    if user:
        entitlement = user.entitlement or UserEntitlement.query.filter_by(user_id=user.id).first()
    expires_at = as_utc(entitlement.access_expires_at) if entitlement else None
    return expires_at if expires_at and expires_at > utc_now() else None


def retention_for_user(user):
    """Return (expiry, tier) for a newly saved output."""
    paid_until = paid_access_expiry(user)
    if paid_until:
        return paid_until + timedelta(days=PAID_AUDIO_GRACE_DAYS), "paid"
    return utc_now() + timedelta(days=FREE_AUDIO_RETENTION_DAYS), "free"


def extend_existing_audio_storage(user):
    """Extend unexpired user audio through paid access and its grace window."""
    paid_until = paid_access_expiry(user)
    if not paid_until:
        return 0
    extended_until = paid_until + timedelta(days=PAID_AUDIO_GRACE_DAYS)
    updated = 0
    for audio in GeneratedAudio.query.filter_by(user_id=user.id, expired=False).all():
        if as_utc(audio.expires_at) > utc_now() and as_utc(audio.expires_at) < extended_until:
            audio.expires_at = extended_until
            audio.storage_tier = "paid"
            updated += 1
    for conversion in Conversion.query.filter_by(user_id=user.id, status="completed", output_expired=False).all():
        if conversion.retention_expires_at:
            current_expiry = as_utc(conversion.retention_expires_at)
            if current_expiry > utc_now() and current_expiry < extended_until:
                conversion.retention_expires_at = extended_until
                conversion.storage_tier = "paid"
                updated += 1
    return updated


def initialize_legacy_conversion_retention():
    """Assign deadlines to successful outputs created before retention tracking."""
    now = utc_now()
    rows = Conversion.query.filter_by(status="completed", output_expired=False).filter(
        Conversion.retention_expires_at.is_(None), Conversion.output_path.is_not(None)
    ).all()
    for conversion in rows:
        paid_until = paid_access_expiry(conversion.user) if conversion.user_id else None
        if paid_until:
            conversion.retention_expires_at = paid_until + timedelta(days=PAID_AUDIO_GRACE_DAYS)
            conversion.storage_tier = "paid"
        else:
            created_at = as_utc(conversion.completed_at or conversion.created_at) or now
            conversion.retention_expires_at = created_at + timedelta(days=FREE_AUDIO_RETENTION_DAYS)
            conversion.storage_tier = "free"
    db.session.commit()
    return len(rows)


def _unlink_if_within(path_value, folder):
    if not path_value:
        return False
    root = Path(folder).resolve()
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    if candidate.is_file():
        candidate.unlink(missing_ok=True)
        return True
    return False


def cleanup_expired_audio(app):
    """Delete expired output/input files while retaining history records."""
    now = utc_now()
    removed_files = 0
    with app.app_context():
        expired_tts = GeneratedAudio.query.filter(
            GeneratedAudio.expired.is_(False), GeneratedAudio.expires_at <= now
        ).all()
        for audio in expired_tts:
            removed_files += _unlink_if_within(
                audio.filename, app.config["OUTPUT_FOLDER"]
            )
            audio.expired = True

        expired_conversions = Conversion.query.filter(
            Conversion.status == "completed",
            Conversion.output_expired.is_(False),
            Conversion.retention_expires_at.is_not(None),
            Conversion.retention_expires_at <= now,
        ).all()
        for conversion in expired_conversions:
            removed_files += _unlink_if_within(
                conversion.output_path, app.config["OUTPUT_FOLDER"]
            )
            removed_files += _unlink_if_within(
                conversion.input_path, app.config["UPLOAD_FOLDER"]
            )
            conversion.output_path = None
            conversion.output_expired = True

        # Free voice previews are temporary, never appear in account history,
        # and are automatically removed after one day.
        temp_root = Path(app.config["TEMP_FOLDER"])
        cutoff = now.timestamp() - 24 * 60 * 60
        if temp_root.is_dir():
            for preview in temp_root.glob("preview-*.mp3"):
                try:
                    if preview.is_file() and preview.stat().st_mtime <= cutoff:
                        preview.unlink(missing_ok=True)
                        removed_files += 1
                except OSError:
                    app.logger.warning("Could not remove expired preview %s", preview)

        db.session.commit()
    return removed_files


def retention_deadline_for_user_id(user_id):
    user = db.session.get(User, user_id) if user_id is not None else None
    return retention_for_user(user)
