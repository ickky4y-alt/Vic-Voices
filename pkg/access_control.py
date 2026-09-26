import hashlib
from datetime import datetime, timedelta, timezone

from flask import current_app, g, has_request_context, request, session
from flask_login import current_user

from .models import AppSetting, DeviceTrial, PaymentRequest, User, UserEntitlement, db

GUEST_TTS_LIMIT = 3
REGISTERED_TTS_LIMIT = 8
REGISTERED_CONVERSION_LIMIT = 5
TRIAL_SETTING_DEFAULTS = {
    "guest_tts": ("guest_tts_trials", GUEST_TTS_LIMIT),
    "tts": ("registered_tts_trials", REGISTERED_TTS_LIMIT),
    "conversion": ("conversion_trials", REGISTERED_CONVERSION_LIMIT),
}


def trial_limit(kind):
    setting_key, default = TRIAL_SETTING_DEFAULTS[kind]
    cache = getattr(g, "trial_limits", None) if has_request_context() else None
    if cache is not None and kind in cache:
        return cache[kind]

    setting = AppSetting.query.filter_by(key=setting_key).first()
    try:
        value = int(setting.value) if setting is not None else default
        if value < 0:
            raise ValueError
    except (TypeError, ValueError):
        value = default

    if has_request_context():
        if cache is None:
            g.trial_limits = {}
            cache = g.trial_limits
        cache[kind] = value
    return value


def utc_now():
    return datetime.now(timezone.utc)


def device_fingerprint():
    if has_request_context():
        raw = "|".join(request.headers.get(key, "") for key in ("User-Agent", "Accept-Language", "Sec-CH-UA", "Sec-CH-UA-Platform"))
    else:
        raw = "test-device-no-request"
    return hashlib.sha256(f"{current_app.config['SECRET_KEY']}|{raw}".encode()).hexdigest()


def get_device_trial():
    fingerprint = device_fingerprint()
    item = DeviceTrial.query.filter_by(fingerprint=fingerprint).first()
    if item is None:
        item = DeviceTrial(fingerprint=fingerprint)
        db.session.add(item)
        db.session.flush()
    item.last_seen_at = utc_now()
    if has_request_context():
        session["device_fingerprint"] = fingerprint
    return item


def get_entitlement(user):
    item = UserEntitlement.query.filter_by(user_id=user.id).first()
    if item is None:
        now = utc_now()
        item = UserEntitlement(user_id=user.id, tts_trials_remaining=trial_limit("tts"), conversion_trials_remaining=trial_limit("conversion"), trial_started_at=now, trial_expires_at=now + timedelta(days=30))
        db.session.add(item)
        db.session.flush()
    return item


def remaining_trials(user, kind):
    entitlement = get_entitlement(user)
    if unlimited_access(entitlement):
        return "Unlimited"
    expires_at = entitlement.trial_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= utc_now():
        return 0
    balance = entitlement.tts_trials_remaining if kind == "tts" else entitlement.conversion_trials_remaining
    return max(0, int(balance or 0))


def unlimited_access(entitlement):
    if not entitlement.access_expires_at:
        return False
    expires_at = entitlement.access_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > utc_now()


def access_state(kind):
    user = current_user._get_current_object() if current_user is not None else None
    if user is not None and getattr(user, "is_authenticated", False):
        entitlement = get_entitlement(user)
        if unlimited_access(entitlement):
            return True, "unlimited", entitlement
        trial_expires_at = entitlement.trial_expires_at
        if trial_expires_at.tzinfo is None:
            trial_expires_at = trial_expires_at.replace(tzinfo=timezone.utc)
        if trial_expires_at <= utc_now():
            return False, "Your free trial has expired. Submit payment to continue.", entitlement
        remaining = entitlement.tts_trials_remaining if kind == "tts" else entitlement.conversion_trials_remaining
        if remaining > 0:
            return True, f"{remaining} trial(s) remaining", entitlement
        return False, "Your free trials are used. Submit payment to continue.", entitlement
    if kind != "tts":
        return False, "Please log in or register to use audio conversion.", None
    device = get_device_trial()
    guest_limit = trial_limit("guest_tts")
    if device.guest_tts_used < guest_limit:
        return True, f"{guest_limit - device.guest_tts_used} guest trial(s) remaining", device
    return False, "This device has already used its free trial. Please log in or register to continue.", device


def consume_success(kind, user_id=None):
    if user_id is not None:
        user = db.session.get(User, user_id)
        if user is None:
            return
    else:
        user = current_user._get_current_object() if current_user is not None else None
    if user is not None and getattr(user, "is_authenticated", False):
        entitlement = get_entitlement(user)
        if not unlimited_access(entitlement):
            if kind == "tts":
                entitlement.tts_trials_remaining = max(0, entitlement.tts_trials_remaining - 1)
            else:
                entitlement.conversion_trials_remaining = max(0, entitlement.conversion_trials_remaining - 1)
    else:
        get_device_trial().guest_tts_used += 1
    db.session.commit()