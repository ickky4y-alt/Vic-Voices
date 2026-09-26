import pkg
from pkg import create_app
from pkg.config import database_uri
from pkg.models import AppSetting, ContactMessage, DeviceTrial, db


def test_app_creates_and_health_works():
    app = create_app({"TESTING": True})
    client = app.test_client()

    response = client.get("/")
    assert response.status_code == 200

    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["status"] == "ok"


def test_public_and_private_pages_render_metadata_and_local_icon():
    app = create_app({"TESTING": True})
    client = app.test_client()

    home = client.get("/")
    html = home.get_data(as_text=True)
    assert '<meta name="description" content="Create expressive text-to-speech audio' in html
    assert '<meta property="og:title" content="VoiceForge | Create with your voice">' in html
    assert '<meta name="twitter:card" content="summary">' in html
    assert 'href="/static/img/vic-voices.svg"' in html
    assert client.get("/static/img/vic-voices.svg").status_code == 200

    login_page = client.get("/login").get_data(as_text=True)
    assert 'name="robots" content="noindex, nofollow, noarchive"' in login_page

    admin_login = client.post("/admin/login", data={"email": "admin1@gmail.com", "password": "Victor@2024"}, follow_redirects=False)
    assert admin_login.status_code == 302
    admin_page = client.get("/admin/").get_data(as_text=True)
    assert 'name="robots" content="noindex, nofollow, noarchive"' in admin_page
    assert 'name="description" content="Private Vic Voices administrator overview' in admin_page


def test_run_uses_stable_server_settings(monkeypatch):
    captured = {}

    class DummyApp:
        def run(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(pkg, "create_app", lambda: DummyApp())
    monkeypatch.setenv("VIC_VOICES_PORT", "5001")
    monkeypatch.delenv("PORT", raising=False)

    import run

    run.main()

    assert captured["debug"] is False
    assert captured["use_reloader"] is False
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 5001


def test_run_uses_railway_port_and_public_bind_address(monkeypatch):
    captured = {}

    class DummyApp:
        def run(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(pkg, "create_app", lambda: DummyApp())
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.delenv("VIC_VOICES_PORT", raising=False)

    import run

    run.main()

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8080


def test_railway_mysql_url_is_normalized_to_supported_driver():
    from urllib.parse import quote

    url = database_uri(f"mysql://railway:{quote('password@with/symbols')}@mysql.railway.internal:3306/railway")
    assert url.startswith("mysql+mysqlconnector://railway:")
    assert "use_pure=true" in url


def test_production_uses_fallback_config_when_env_vars_missing(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    app = create_app({"TESTING": True})
    assert app.config["SECRET_KEY"] is not None
    assert app.config["ADMIN_EMAIL"] is not None

    monkeypatch.delenv("APP_ENV")


def test_default_admin_fallback_is_created():
    app = create_app({"TESTING": True})
    with app.app_context():
        from pkg.models import User
        admin = User.query.filter_by(email="admin1@gmail.com").first()
        assert admin is not None
        assert admin.is_admin is True
        assert admin.active is True


def test_homepage_uses_admin_pricing_settings():
    app = create_app({"TESTING": True})
    with app.app_context():
        AppSetting.query.filter_by(key="billing_currency").delete()
        AppSetting.query.filter_by(key="monthly_sale_price").delete()
        AppSetting.query.filter_by(key="quarterly_sale_price").delete()
        AppSetting.query.filter_by(key="annual_sale_price").delete()
        db.session.add_all([
            AppSetting(key="billing_currency", value="NGN"),
            AppSetting(key="monthly_sale_price", value="2500"),
            AppSetting(key="quarterly_sale_price", value="6500"),
            AppSetting(key="annual_sale_price", value="18000"),
        ])
        db.session.commit()

    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "NGN" in html
    assert "2500" in html
    assert "6500" in html
    assert "18000" in html


def test_homepage_uses_configured_visitor_and_account_trial_limits():
    app = create_app({"TESTING": True})
    with app.app_context():
        AppSetting.query.filter(AppSetting.key.in_(["guest_tts_trials", "registered_tts_trials", "conversion_trials"])).delete(synchronize_session=False)
        db.session.add_all([
            AppSetting(key="guest_tts_trials", value="6"),
            AppSetting(key="registered_tts_trials", value="12"),
            AppSetting(key="conversion_trials", value="9"),
        ])
        db.session.commit()

    response = app.test_client().get("/")
    assert response.status_code == 200
    assert b"6 free speech creations as a visitor" in response.data
    assert b"unlock 12 TTS generations and 9 Vic RVC audio conversions" in response.data


def test_guest_tts_uses_three_free_trials():
    app = create_app({"TESTING": True})
    client = app.test_client()
    with app.app_context():
        DeviceTrial.query.delete()
        db.session.commit()

    with app.app_context():
        from pkg.access_control import access_state, consume_success, get_device_trial

        for i in range(3):
            allowed, reason, device = access_state("tts")
            assert allowed is True, reason
            consume_success("tts")
            assert device.guest_tts_used == i + 1

        blocked, reason, _ = access_state("tts")
        assert blocked is False
        assert "free trial" in reason.lower()


def test_contact_message_reaches_admin_inbox():
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})
    client = app.test_client()
    response = client.post("/contact", data={"name": "Guest Creator", "email": "creator@example.com", "category": "website", "subject": "Website project", "message": "I need a new business website."}, follow_redirects=False)
    assert response.status_code == 302
    with app.app_context():
        message = ContactMessage.query.filter_by(email="creator@example.com").one()
        assert message.status == "open"
    admin = app.test_client()
    admin.post("/admin/login", data={"email": "admin1@gmail.com", "password": "Victor@2024"}, follow_redirects=False)
    inbox = admin.get("/admin/contact-messages")
    assert inbox.status_code == 200
    assert b"Website project" in inbox.data
