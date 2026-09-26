from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from pkg import create_app
from pkg.models import AppSetting, User, UserEntitlement, db


def create_admin_user(app):
    with app.app_context():
        user = User.query.filter_by(email="admin@example.com").first()
        if not user:
            user = User(name="Admin", email="admin@example.com", password_hash=generate_password_hash("AdminPass123!"), role="admin", active=True, is_admin=True)
            db.session.add(user)
            db.session.commit()
        return user


def test_non_admin_cant_access_admin_routes():
    app = create_app({"TESTING": True})
    with app.app_context():
        user = User(name="User", email="user@example.com", password_hash=generate_password_hash("secret123"), role="user", active=True)
        db.session.add(user)
        db.session.commit()
    client = app.test_client()
    client.post("/login", data={"email": "user@example.com", "password": "secret123"}, follow_redirects=False)
    resp = client.get("/admin/")
    assert resp.status_code in (302, 403)


def test_admin_login_and_dashboard_access():
    app = create_app({"TESTING": True})
    create_admin_user(app)
    client = app.test_client()
    resp = client.post("/admin/login", data={"email": "admin@example.com", "password": "AdminPass123!"}, follow_redirects=True)
    assert resp.status_code == 200
    dashboard = client.get("/admin/")
    assert dashboard.status_code == 200
    assert "no-store" in dashboard.headers["Cache-Control"]
    payments = client.get("/admin/payments")
    assert payments.status_code == 200
    assert b"Payment review" in payments.data

    with client.session_transaction() as flask_session:
        assert "_user_id" in flask_session
    logout = client.get("/admin/logout")
    assert logout.status_code == 302
    with client.session_transaction() as flask_session:
        assert "_user_id" not in flask_session
    assert client.get("/admin/", follow_redirects=False).status_code == 302


def test_admin_can_update_trial_limits_without_resetting_existing_balances():
    app = create_app({"TESTING": True})
    create_admin_user(app)
    with app.app_context():
        user = User(name="Trial User", email="trial@example.com", password_hash=generate_password_hash("TrialPass123!"), active=True)
        db.session.add(user)
        db.session.flush()
        entitlement = UserEntitlement(user_id=user.id, tts_trials_remaining=6, conversion_trials_remaining=4, trial_expires_at=datetime.now(timezone.utc) + timedelta(days=30))
        db.session.add(entitlement)
        db.session.commit()
        user_id = user.id

    client = app.test_client()
    client.post("/admin/login", data={"email": "admin@example.com", "password": "AdminPass123!"}, follow_redirects=False)
    response = client.post(
        "/admin/settings",
        data={"guest_tts_trials": "2", "registered_tts_trials": "2", "conversion_trials": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 200

    with app.app_context():
        assert AppSetting.query.filter_by(key="guest_tts_trials").one().value == "2"
        assert AppSetting.query.filter_by(key="registered_tts_trials").one().value == "2"
        assert AppSetting.query.filter_by(key="conversion_trials").one().value == "1"
        user = db.session.get(User, user_id)
        with app.test_request_context("/"):
            from flask_login import login_user
            from pkg.access_control import access_state

            login_user(user)
            assert access_state("tts")[:2] == (True, "6 trial(s) remaining")
            assert access_state("conversion")[:2] == (True, "4 trial(s) remaining")

        assert user.entitlement.tts_trials_remaining == 6
        assert user.entitlement.conversion_trials_remaining == 4

        new_user = User(name="New Trial User", email="new-trial@example.com", password_hash="unused", active=True)
        db.session.add(new_user)
        db.session.flush()
        from pkg.access_control import get_entitlement

        new_entitlement = get_entitlement(new_user)
        assert new_entitlement.tts_trials_remaining == 2
        assert new_entitlement.conversion_trials_remaining == 1

    with app.test_request_context("/"):
        from pkg.access_control import access_state

        assert access_state("tts")[:2] == (True, "2 guest trial(s) remaining")

    invalid_response = client.post(
        "/admin/settings",
        data={"guest_tts_trials": "-1", "registered_tts_trials": "2", "conversion_trials": "1"},
        follow_redirects=False,
    )
    assert invalid_response.status_code == 200
    with app.app_context():
        assert AppSetting.query.filter_by(key="guest_tts_trials").one().value == "2"
