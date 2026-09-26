import io
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from werkzeug.security import generate_password_hash

from pkg import create_app
from pkg.models import Conversion, DeviceTrial, GeneratedAudio, PaymentRequest, User, UserActivity, UserEntitlement, VoiceModel, db


def make_app():
    return create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})


def add_user(email, password="StrongPass123!", active=True):
    user = User(name=email.split("@")[0], email=email, password_hash=generate_password_hash(password), active=active)
    db.session.add(user)
    db.session.commit()
    return user


def login(client, email, password="StrongPass123!"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_registration_creates_user_activity_and_dashboard():
    app = make_app()
    client = app.test_client()
    response = client.post("/register", data={"name": "New User", "email": "NEW@Example.com", "password": "StrongPass123!", "confirm_password": "StrongPass123!"}, follow_redirects=False)
    assert response.status_code == 302
    with app.app_context():
        user = User.query.filter_by(email="new@example.com").first()
        assert user is not None
        assert user.password_hash != "StrongPass123!"
        assert user.entitlement.tts_trials_remaining == 8
        assert user.entitlement.conversion_trials_remaining == 5
        assert UserActivity.query.filter_by(user_id=user.id, activity_type="account_created").count() == 1
    assert client.get("/dashboard").status_code == 200


def test_suspended_user_cannot_login():
    app = make_app()
    with app.app_context():
        add_user("suspended@example.com", active=False)
    client = app.test_client()
    response = login(client, "suspended@example.com")
    assert response.status_code == 200
    assert b"disabled" in response.data.lower()
    assert client.get("/dashboard").status_code == 302


def test_logout_clears_session_and_prevents_cached_account_access():
    app = make_app()
    with app.app_context():
        add_user("logout@example.com")
    client = app.test_client()

    assert login(client, "logout@example.com").status_code == 302
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "no-store" in dashboard.headers["Cache-Control"]
    with client.session_transaction() as flask_session:
        assert "_user_id" in flask_session

    logout_response = client.get("/logout")
    assert logout_response.status_code == 302
    assert "no-store" in logout_response.headers["Cache-Control"]
    with client.session_transaction() as flask_session:
        assert "_user_id" not in flask_session

    protected_response = client.get("/dashboard", follow_redirects=False)
    assert protected_response.status_code == 302
    assert "/login" in protected_response.headers["Location"]
    converter_response = client.get("/converter", follow_redirects=False)
    assert converter_response.status_code == 302
    assert "/login?next=%2Fconverter" in converter_response.headers["Location"]


def test_homepage_hides_guest_tts_for_authenticated_users():
    app = make_app()
    with app.app_context():
        add_user("homepage@example.com")
    client = app.test_client()

    guest_home = client.get("/")
    assert b"3 free speech creations as a visitor" in guest_home.data
    assert b"Create a free account to unlock 8 TTS generations and 5 Vic RVC audio conversions" in guest_home.data
    assert b"tts-section" in guest_home.data

    login(client, "homepage@example.com")
    user_home = client.get("/")
    assert user_home.status_code == 200
    assert b"Write it" in user_home.data
    assert b"Open converter" in user_home.data
    assert b"3 free speech creations as a visitor" not in user_home.data
    assert b"TTS:" in user_home.data
    assert b"8" in user_home.data
    assert b"Audio conversion:" in user_home.data


def test_payment_plan_receipt_and_admin_confirmation_grant_duration():
    app = make_app()
    with app.app_context():
        user = add_user("payment@example.com")
        user_id = user.id
    client = app.test_client()
    login(client, "payment@example.com")
    billing_page = client.get("/billing")
    assert billing_page.status_code == 200
    assert b"Pay now" in billing_page.data
    intent = client.post("/billing/payment-intent", json={"plan": "quarterly"})
    assert intent.status_code == 200
    payment_data = intent.get_json()
    assert payment_data["plan"] == "Quarterly"
    assert payment_data["reference_code"].startswith("VF-")
    response = client.post(
        "/billing",
        data={
            "payment_id": payment_data["payment_id"],
            "reference": payment_data["reference_code"],
            "sender_name": "Payment Sender",
            "notes": "Bank transfer",
            "receipt": (io.BytesIO(b"receipt"), "receipt.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302
    pending_billing = client.get("/billing")
    assert pending_billing.status_code == 200
    assert payment_data["reference_code"].encode() in pending_billing.data
    with app.app_context():
        payment = PaymentRequest.query.filter_by(user_id=user_id).one()
        assert payment.plan_key == "quarterly"
        assert payment.status == "pending"
        payment_id = payment.id
    admin_client = app.test_client()
    login(admin_client, "admin1@gmail.com", "Victor@2024")
    reviewed = admin_client.post(f"/admin/payments/{payment_id}/confirm", follow_redirects=False)
    assert reviewed.status_code == 302
    with app.app_context():
        entitlement = UserEntitlement.query.filter_by(user_id=user_id).one()
        assert entitlement.payment_status == "confirmed"
        assert (entitlement.access_expires_at - entitlement.access_started_at).days == 90


def test_billing_renders_paid_access_from_sqlite_timestamp():
    app = make_app()
    with app.app_context():
        user = add_user("paid-access@example.com")
        entitlement = UserEntitlement(user_id=user.id, tts_trials_remaining=0, conversion_trials_remaining=0, trial_expires_at=datetime.now(timezone.utc) + timedelta(days=30), access_started_at=datetime.now(timezone.utc), access_expires_at=datetime.now(timezone.utc) + timedelta(days=30), payment_status="confirmed")
        db.session.add(entitlement)
        db.session.commit()
    client = app.test_client()
    login(client, "paid-access@example.com")
    response = client.get("/billing")
    assert response.status_code == 200
    assert b"Active" in response.data


def test_conversion_duration_handles_sqlite_naive_timestamps():
    from pkg.user_routes import elapsed_seconds

    started = datetime(2026, 1, 1, 12, 0, 0)
    completed = datetime(2026, 1, 1, 12, 0, 8, tzinfo=timezone.utc)
    assert elapsed_seconds(started, completed) == 8


def test_conversion_ownership_blocks_idor():
    app = make_app()
    with app.app_context():
        owner = add_user("owner@example.com")
        other = add_user("other@example.com")
        conversion = Conversion(user_id=owner.id, voice_name="Test", input_path="", status="failed", created_at=datetime.utcnow())
        db.session.add(conversion)
        db.session.commit()
        conversion_id = conversion.id
    client = app.test_client()
    login(client, "other@example.com")
    assert client.get(f"/history/{conversion_id}").status_code == 404
    assert client.get(f"/download/{conversion_id}").status_code == 404


def test_activity_favorite_and_delete_account_are_user_scoped():
    app = make_app()
    with app.app_context():
        add_user("account@example.com")
    client = app.test_client()
    login(client, "account@example.com")
    assert client.get("/activity").status_code == 200
    assert client.get("/profile").status_code == 200
    response = client.post("/account/delete", data={"confirm": "DELETE"}, follow_redirects=False)
    assert response.status_code == 302
    with app.app_context():
        assert User.query.filter_by(email="account@example.com").first() is None


def test_free_tts_voice_preview_never_consumes_trial(monkeypatch, tmp_path):
    app = make_app()
    app.config["TEMP_FOLDER"] = str(tmp_path)
    with app.app_context():
        user = add_user("preview@example.com")
        entitlement = UserEntitlement(user_id=user.id, tts_trials_remaining=8, conversion_trials_remaining=5, trial_expires_at=datetime.now(timezone.utc) + timedelta(days=30))
        db.session.add(entitlement)
        db.session.commit()

    generated = []

    def fake_generate(text, voice, output_path, ffmpeg_path=None, style="neutral", speed=None):
        generated.append((text, voice, style))
        Path(output_path).write_bytes(b"preview audio")

    monkeypatch.setattr("pkg.user_routes.generate_speech", fake_generate)
    client = app.test_client()
    response = client.post("/api/tts/preview", json={"voice": "af_heart", "style": "calm"})
    assert response.status_code == 200
    assert response.get_json()["audio_url"].startswith("/preview-audio/")
    assert generated and generated[0][0] == "Take a slow breath. Everything is going to be okay."

    login(client, "preview@example.com")
    response = client.post("/api/tts/preview", json={"voice": "af_heart", "style": "neutral"})
    assert response.status_code == 200
    with app.app_context():
        user = User.query.filter_by(email="preview@example.com").one()
        assert user.entitlement.tts_trials_remaining == 8
        assert user.entitlement.conversion_trials_remaining == 5
        assert DeviceTrial.query.count() == 0
        assert GeneratedAudio.query.count() == 0


def test_successful_tts_generation_consumes_one_trial_and_saves_audio(monkeypatch, tmp_path):
    app = make_app()
    app.config["OUTPUT_FOLDER"] = str(tmp_path)
    with app.app_context():
        add_user("tts-usage@example.com")

    should_fail = {"value": False}

    def fake_generate(text, voice, output_path, ffmpeg_path=None, style="neutral", speed=None):
        if should_fail["value"]:
            raise RuntimeError("synthetic generation failure")
        Path(output_path).write_bytes(b"audio")

    monkeypatch.setattr("pkg.user_routes.generate_speech", fake_generate)
    client = app.test_client()
    login(client, "tts-usage@example.com")
    response = client.post("/api/tts", json={"text": "Hello there", "voice": "af_heart", "style": "neutral"})
    assert response.status_code == 200
    with app.app_context():
        user = User.query.filter_by(email="tts-usage@example.com").one()
        assert user.entitlement.tts_trials_remaining == 7
        saved_audio = GeneratedAudio.query.filter_by(user_id=user.id).one()
        assert saved_audio.filename.endswith(".mp3")
        assert saved_audio.storage_tier == "free"
        expiry = saved_audio.expires_at.replace(tzinfo=timezone.utc)
        created = saved_audio.created_at.replace(tzinfo=timezone.utc)
        assert (expiry - created).days in (6, 7)

    history_page = client.get("/history")
    assert history_page.status_code == 200
    assert b"Generated TTS audio" in history_page.data
    assert b"7-day free storage" in history_page.data

    should_fail["value"] = True
    failed_response = client.post("/api/tts", json={"text": "Try again", "voice": "af_heart", "style": "neutral"})
    assert failed_response.status_code == 503
    with app.app_context():
        user = User.query.filter_by(email="tts-usage@example.com").one()
        assert user.entitlement.tts_trials_remaining == 7


def test_only_successful_background_conversion_consumes_owner_trial(tmp_path, monkeypatch):
    app = make_app()
    app.config["OUTPUT_FOLDER"] = str(tmp_path)
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    with app.app_context():
        user = add_user("conversion-usage@example.com")
        entitlement = UserEntitlement(user_id=user.id, tts_trials_remaining=8, conversion_trials_remaining=5, trial_expires_at=datetime.now(timezone.utc) + timedelta(days=30))
        db.session.add(entitlement)
        successful = Conversion(user_id=user.id, voice_name="Test voice", input_path="", output_path=str(tmp_path / "successful.wav"), status="processing", started_at=datetime.now(timezone.utc))
        failed = Conversion(user_id=user.id, voice_name="Test voice", input_path="", output_path=str(tmp_path / "failed.wav"), status="processing", started_at=datetime.now(timezone.utc))
        db.session.add_all([successful, failed])
        db.session.commit()
        successful_id, failed_id, user_id = successful.id, failed.id, user.id

    class FakeRVCService:
        def convert(self, _stored, _name, output, **_kwargs):
            if "failed" in output:
                raise RuntimeError("synthetic conversion failure")
            Path(output).write_bytes(b"converted")

    monkeypatch.setattr("pkg.user_routes.RVCService", FakeRVCService)
    from pkg.user_routes import run_conversion

    form_data = {"pitch": 0, "f0_method": "pm", "index_rate": 0.0, "protect": 0.33, "output_format": "wav"}
    run_conversion(app, successful_id, user_id, "input.wav", tmp_path / "input.wav", tmp_path / "successful.wav", {"name": "Test voice"}, form_data)
    run_conversion(app, failed_id, user_id, "input.wav", tmp_path / "input.wav", tmp_path / "failed.wav", {"name": "Test voice"}, form_data)

    with app.app_context():
        user = db.session.get(User, user_id)
        assert user.entitlement.conversion_trials_remaining == 4
        assert user.entitlement.tts_trials_remaining == 8
        assert db.session.get(Conversion, successful_id).status == "completed"
        assert db.session.get(Conversion, failed_id).status == "failed"
        saved_conversion = db.session.get(Conversion, successful_id)
        assert saved_conversion.retention_expires_at is not None
        assert saved_conversion.storage_tier == "free"


def test_voice_library_is_public_and_preserves_rvc_model_through_login():
    app = make_app()
    with app.app_context():
        model = VoiceModel(name="Library test RVC", path="library-test.pth", enabled=True, status="active", visibility="public", validation_status="valid")
        db.session.add(model)
        db.session.commit()
        model_id = model.id

    guest_client = app.test_client()
    guest_page = guest_client.get("/voice-library")
    assert guest_page.status_code == 200
    assert b"Vic TTS voices" in guest_page.data
    assert b"Vic RVC audio conversion models" in guest_page.data
    assert b"Preview listening does not use your TTS or conversion balance" in guest_page.data
    assert b"Library test RVC" in guest_page.data
    guest_html = guest_page.get_data(as_text=True)
    guest_use_links = re.findall(r'href="([^"]+)"[^>]*>Use voice</a>', guest_html)
    guest_return_targets = [parse_qs(urlparse(link).query).get("next", [""])[0] for link in guest_use_links]
    assert f"/converter?model_id={model_id}" in guest_return_targets

    with app.app_context():
        add_user("library-user@example.com")
    user_client = app.test_client()
    login(user_client, "library-user@example.com")
    user_page = user_client.get("/voice-library")
    assert user_page.status_code == 200
    user_html = user_page.get_data(as_text=True)
    user_use_links = re.findall(r'href="([^"]+)"[^>]*>Use voice</a>', user_html)
    assert f"/converter?model_id={model_id}" in user_use_links


    def test_paid_access_extends_existing_audio_through_seven_day_grace(tmp_path):
        app = make_app()
        app.config["OUTPUT_FOLDER"] = str(tmp_path)
        app.config["UPLOAD_FOLDER"] = str(tmp_path)
        with app.app_context():
            user = add_user("storage-upgrade@example.com")
            now = datetime.now(timezone.utc)
            entitlement = UserEntitlement(
                user_id=user.id,
                tts_trials_remaining=0,
                conversion_trials_remaining=0,
                trial_expires_at=now + timedelta(days=30),
                access_started_at=now,
                access_expires_at=now + timedelta(days=30),
                payment_status="confirmed",
            )
            db.session.add(entitlement)
            db.session.flush()
            tts_audio = GeneratedAudio(user_id=user.id, filename="saved.mp3", voice_name="Heart", style_name="neutral", created_at=now, expires_at=now + timedelta(days=2), storage_tier="free")
            conversion = Conversion(user_id=user.id, voice_name="RVC voice", input_path=str(tmp_path / "input.wav"), output_path=str(tmp_path / "converted.wav"), status="completed", created_at=now, retention_expires_at=now + timedelta(days=3), storage_tier="free")
            db.session.add_all([tts_audio, conversion])
            db.session.commit()

            from pkg.audio_retention import extend_existing_audio_storage

            updated = extend_existing_audio_storage(user)
            grace_deadline = entitlement.access_expires_at.replace(tzinfo=timezone.utc) + timedelta(days=7)
            assert updated == 2
            assert tts_audio.storage_tier == "paid"
            assert conversion.storage_tier == "paid"
            assert tts_audio.expires_at.replace(tzinfo=timezone.utc) == grace_deadline
            assert conversion.retention_expires_at.replace(tzinfo=timezone.utc) == grace_deadline


    def test_expired_audio_files_are_removed_but_history_is_kept(tmp_path):
        app = make_app()
        app.config["OUTPUT_FOLDER"] = str(tmp_path / "outputs")
        app.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
        Path(app.config["OUTPUT_FOLDER"]).mkdir()
        Path(app.config["UPLOAD_FOLDER"]).mkdir()
        output_path = Path(app.config["OUTPUT_FOLDER"]) / "expired.mp3"
        converted_path = Path(app.config["OUTPUT_FOLDER"]) / "expired.wav"
        input_path = Path(app.config["UPLOAD_FOLDER"]) / "source.wav"
        output_path.write_bytes(b"expired tts")
        converted_path.write_bytes(b"expired conversion")
        input_path.write_bytes(b"source audio")

        app.config.update(TESTING=True)
        with app.app_context():
            user = add_user("expired-audio@example.com")
            expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            tts_audio = GeneratedAudio(user_id=user.id, filename=output_path.name, voice_name="Heart", style_name="neutral", expires_at=expired_at, created_at=expired_at - timedelta(days=7), storage_tier="free")
            conversion = Conversion(user_id=user.id, voice_name="RVC voice", input_path=str(input_path), output_path=str(converted_path), status="completed", created_at=expired_at - timedelta(days=7), retention_expires_at=expired_at, storage_tier="free")
            db.session.add_all([tts_audio, conversion])
            db.session.commit()
            audio_id, conversion_id = tts_audio.id, conversion.id

        from pkg.audio_retention import cleanup_expired_audio

        cleanup_expired_audio(app)
        assert not output_path.exists()
        assert not converted_path.exists()
        assert not input_path.exists()
        with app.app_context():
            expired_audio = db.session.get(GeneratedAudio, audio_id)
            expired_conversion = db.session.get(Conversion, conversion_id)
            assert expired_audio.expired is True
            assert expired_conversion.status == "completed"
            assert expired_conversion.output_expired is True
            assert expired_conversion.output_path is None

        client = app.test_client()
        login(client, "expired-audio@example.com")
        history_page = client.get("/history")
        assert b"Audio expired and was removed" in history_page.data
        assert b"Expired" in history_page.data
