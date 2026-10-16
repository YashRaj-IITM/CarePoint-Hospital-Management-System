from datetime import datetime, timedelta

import pytest
from flask_security import hash_password

from app import create_app
from models import db, User, Doctor, Patient, Appointment, DoctorAvailability, Treatment, ReminderLog
from reminders import send_due_reminders


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "test.db"
    application = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret-key-which-is-long-enough-12345",
        "SECURITY_PASSWORD_SALT": "test-password-salt-which-is-long-enough-12345",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        "WTF_CSRF_ENABLED": False,
        "ADMIN_EMAIL": "admin@example.com",
        "ADMIN_PASSWORD": "StrongTestAdminPassword123!",
    })
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def register(client, email="patient@example.com", name="Test Patient"):
    return client.post("/api/auth/register", json={
        "full_name": name, "email": email, "password": "SecurePass123"
    })


def login(client, email, password):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.get_json()
    return {"Authentication-Token": response.get_json()["token"]}


def admin_headers(client):
    return login(client, "admin@example.com", "StrongTestAdminPassword123!")


def create_doctor(client, headers, account=True, name="Dr Test"):
    body = {"full_name": name, "specialization": "General Medicine", "available": True}
    if account:
        body.update({"account_email": "doctor@example.com", "account_password": "StrongDoctorPassword123!"})
    response = client.post("/api/doctors", json=body, headers=headers)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def future_datetime(days=2, hour=10):
    return (datetime.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_register_login_and_me(client):
    assert register(client).status_code == 201
    headers = login(client, "patient@example.com", "SecurePass123")
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.get_json()["email"] == "patient@example.com"
    assert "patient" in me.get_json()["roles"]


def test_duplicate_registration_rejected(client):
    assert register(client).status_code == 201
    assert register(client).status_code == 409


def test_protected_endpoint_requires_token(client):
    response = client.get("/api/appointments")
    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized"


def test_admin_can_create_department(client):
    response = client.post("/api/departments", json={"name": "Neurology"}, headers=admin_headers(client))
    assert response.status_code == 201
    assert response.get_json()["name"] == "Neurology"


def test_patient_cannot_create_department(client):
    register(client)
    headers = login(client, "patient@example.com", "SecurePass123")
    response = client.post("/api/departments", json={"name": "Restricted"}, headers=headers)
    assert response.status_code == 403


def test_admin_can_create_doctor_login_account(client):
    doctor = create_doctor(client, admin_headers(client))
    assert doctor["login_account_created"] is True
    doctor_headers = login(client, "doctor@example.com", "StrongDoctorPassword123!")
    me = client.get("/api/auth/me", headers=doctor_headers)
    assert me.status_code == 200
    assert "doctor" in me.get_json()["roles"]


def test_doctor_account_requires_both_email_and_password(client):
    response = client.post("/api/doctors", json={
        "full_name": "Dr Missing Credentials", "specialization": "Cardiology", "account_email": "doctor@example.com"
    }, headers=admin_headers(client))
    assert response.status_code == 400


def test_patient_can_update_own_profile(client):
    register(client)
    headers = login(client, "patient@example.com", "SecurePass123")
    response = client.put("/api/patients/me", json={"full_name": "Updated Name", "phone": "1234567890"}, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["full_name"] == "Updated Name"


def test_availability_overlap_is_rejected(client):
    headers = admin_headers(client)
    doctor = create_doctor(client, headers, account=False)
    slot_date = (datetime.now() + timedelta(days=3)).date().isoformat()
    first = client.post(f"/api/doctors/{doctor['id']}/availability", json={
        "date": slot_date, "start_time": "09:00", "end_time": "12:00"
    }, headers=headers)
    assert first.status_code == 201, first.get_json()
    overlap = client.post(f"/api/doctors/{doctor['id']}/availability", json={
        "date": slot_date, "start_time": "11:00", "end_time": "13:00"
    }, headers=headers)
    assert overlap.status_code == 409


def test_appointment_must_fit_configured_availability(client):
    admin = admin_headers(client)
    doctor = create_doctor(client, admin, account=False)
    register(client)
    patient = login(client, "patient@example.com", "SecurePass123")
    when = future_datetime(days=4, hour=15)
    client.post(f"/api/doctors/{doctor['id']}/availability", json={
        "date": when.date().isoformat(), "start_time": "09:00", "end_time": "12:00"
    }, headers=admin)
    response = client.post("/api/appointments", json={
        "doctor_id": doctor["id"], "scheduled_at": when.isoformat(), "reason": "Checkup"
    }, headers=patient)
    assert response.status_code == 409


def test_appointment_reschedule_rejects_past_time(client):
    admin = admin_headers(client)
    doctor = create_doctor(client, admin, account=False)
    register(client)
    patient = login(client, "patient@example.com", "SecurePass123")
    created = client.post("/api/appointments", json={
        "doctor_id": doctor["id"], "scheduled_at": future_datetime().isoformat()
    }, headers=patient)
    assert created.status_code == 201, created.get_json()
    response = client.put(f"/api/appointments/{created.get_json()['id']}", json={
        "scheduled_at": (datetime.now() - timedelta(days=1)).isoformat()
    }, headers=patient)
    assert response.status_code == 400


def test_appointment_reschedule_rejects_conflict(client):
    admin = admin_headers(client)
    doctor = create_doctor(client, admin, account=False)
    register(client, "patient1@example.com", "Patient One")
    register(client, "patient2@example.com", "Patient Two")
    patient1 = login(client, "patient1@example.com", "SecurePass123")
    patient2 = login(client, "patient2@example.com", "SecurePass123")
    slot = future_datetime(days=5).isoformat()
    first = client.post("/api/appointments", json={"doctor_id": doctor["id"], "scheduled_at": slot}, headers=patient1)
    second = client.post("/api/appointments", json={"doctor_id": doctor["id"], "scheduled_at": future_datetime(days=6).isoformat()}, headers=patient2)
    assert first.status_code == second.status_code == 201
    response = client.put(f"/api/appointments/{second.get_json()['id']}", json={"scheduled_at": slot}, headers=patient2)
    assert response.status_code == 409


def test_patient_cannot_read_another_patients_appointment(client, app):
    admin = admin_headers(client)
    doctor = create_doctor(client, admin, account=False)
    register(client, "owner@example.com", "Owner")
    register(client, "other@example.com", "Other")
    owner = login(client, "owner@example.com", "SecurePass123")
    other = login(client, "other@example.com", "SecurePass123")
    response = client.post("/api/appointments", json={"doctor_id": doctor["id"], "scheduled_at": future_datetime().isoformat()}, headers=owner)
    assert response.status_code == 201
    appointment_id = response.get_json()["id"]
    denied = client.get("/api/appointments", headers=other)
    assert denied.status_code == 200 and all(a["id"] != appointment_id for a in denied.get_json())
    assert client.get(f"/api/appointments/{appointment_id}/treatments", headers=other).status_code == 403


def test_assigned_doctor_can_record_treatment(client):
    admin = admin_headers(client)
    doctor = create_doctor(client, admin, account=True)
    register(client)
    patient = login(client, "patient@example.com", "SecurePass123")
    created = client.post("/api/appointments", json={"doctor_id": doctor["id"], "scheduled_at": future_datetime().isoformat()}, headers=patient)
    assert created.status_code == 201, created.get_json()
    doctor_token = login(client, "doctor@example.com", "StrongDoctorPassword123!")
    treatment = client.post(f"/api/appointments/{created.get_json()['id']}/treatments", json={
        "diagnosis": "Routine check", "prescription": "Rest and fluids", "notes": "Follow up if needed"
    }, headers=doctor_token)
    assert treatment.status_code == 201, treatment.get_json()
    assert treatment.get_json()["diagnosis"] == "Routine check"
    assert client.get(f"/api/appointments/{created.get_json()['id']}/treatments", headers=patient).status_code == 200


def test_patient_cannot_record_treatment(client):
    admin = admin_headers(client)
    doctor = create_doctor(client, admin, account=False)
    register(client)
    patient = login(client, "patient@example.com", "SecurePass123")
    created = client.post("/api/appointments", json={"doctor_id": doctor["id"], "scheduled_at": future_datetime().isoformat()}, headers=patient)
    response = client.post(f"/api/appointments/{created.get_json()['id']}/treatments", json={"diagnosis": "Not allowed"}, headers=patient)
    assert response.status_code == 403


def test_admin_can_update_and_delete_department(client):
    headers = admin_headers(client)
    created = client.post("/api/departments", json={"name": "Neurology"}, headers=headers).get_json()
    updated = client.put(f"/api/departments/{created['id']}", json={"description": "Brain care"}, headers=headers)
    assert updated.status_code == 200 and updated.get_json()["description"] == "Brain care"
    duplicate = client.put(f"/api/departments/{created['id']}", json={"name": "Cardiology"}, headers=headers)
    assert duplicate.status_code == 409
    assert client.delete(f"/api/departments/{created['id']}", headers=headers).status_code == 204


def test_logout_invalidates_token(client):
    register(client)
    headers = login(client, "patient@example.com", "SecurePass123")
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_appointment_time_is_returned_as_utc(client):
    admin = admin_headers(client)
    doctor = create_doctor(client, admin, account=False)
    register(client)
    headers = login(client, "patient@example.com", "SecurePass123")
    response = client.post("/api/appointments", json={
        "doctor_id": doctor["id"], "scheduled_at": future_datetime().isoformat()
    }, headers=headers)
    assert response.status_code == 201
    assert response.get_json()["scheduled_at"].endswith("Z")


def test_admin_can_edit_doctor_and_clear_department(client):
    headers = admin_headers(client)
    department = client.get("/api/departments").get_json()[0]
    doctor = create_doctor(client, headers, account=False)
    updated = client.put(f"/api/doctors/{doctor['id']}", json={
        "full_name": "Dr Renamed", "specialization": "Cardiologist", "phone": "555-0100",
        "department_id": department["id"], "available": False,
    }, headers=headers)
    body = updated.get_json()
    assert updated.status_code == 200
    assert body["full_name"] == "Dr Renamed" and body["department_id"] == department["id"] and body["available"] is False
    cleared = client.put(f"/api/doctors/{doctor['id']}", json={"department_id": ""}, headers=headers)
    assert cleared.status_code == 200 and cleared.get_json()["department_id"] is None


def test_doctor_edit_validation_and_permissions(client):
    headers = admin_headers(client)
    doctor = create_doctor(client, headers, account=False)
    assert client.put(f"/api/doctors/{doctor['id']}", json={"full_name": "  "}, headers=headers).status_code == 400
    assert client.put(f"/api/doctors/{doctor['id']}", json={"department_id": 9999}, headers=headers).status_code == 404
    register(client)
    patient = login(client, "patient@example.com", "SecurePass123")
    assert client.put(f"/api/doctors/{doctor['id']}", json={"full_name": "Hacker"}, headers=patient).status_code == 403


def _book_directly(app, hours_from_now, status="scheduled"):
    """Create doctor + patient + appointment straight in the DB (bypasses the future-time API check)."""
    with app.app_context():
        doctor = Doctor(full_name="Dr Reminder", specialization="GP")
        user = User(email="remind@example.com", password="x", full_name="Remi Patient", fs_uniquifier="remind-uniq")
        db.session.add_all([doctor, user]); db.session.flush()
        patient = Patient(user_id=user.id, full_name="Remi Patient", email="remind@example.com")
        db.session.add(patient); db.session.flush()
        appointment = Appointment(patient_id=patient.id, doctor_id=doctor.id, status=status,
                                  scheduled_at=datetime.utcnow() + timedelta(hours=hours_from_now))
        db.session.add(appointment); db.session.commit()
        return appointment.id


def test_reminder_sent_once_for_appointment_inside_window(app):
    appointment_id = _book_directly(app, hours_from_now=3)
    with app.app_context():
        assert send_due_reminders() == 1
        assert send_due_reminders() == 0  # already reminded
        log = ReminderLog.query.filter_by(appointment_id=appointment_id).one()
        assert log.recipient == "remind@example.com" and log.channel == "log"


def test_reminder_skips_far_future_and_cancelled_appointments(app):
    _book_directly(app, hours_from_now=72)
    with app.app_context():
        assert send_due_reminders() == 0
    with app.app_context():
        appointment = Appointment.query.first()
        appointment.scheduled_at = datetime.utcnow() + timedelta(hours=2)
        appointment.status = "cancelled"
        db.session.commit()
        assert send_due_reminders() == 0


def test_reminder_endpoints_are_admin_only_and_work(client, app):
    _book_directly(app, hours_from_now=5)
    register(client)
    patient = login(client, "patient@example.com", "SecurePass123")
    assert client.post("/api/reminders/run", headers=patient).status_code == 403
    assert client.get("/api/reminders", headers=patient).status_code == 403
    assert client.post("/api/reminders/run").status_code == 401
    admin = admin_headers(client)
    run = client.post("/api/reminders/run", headers=admin)
    assert run.status_code == 200 and run.get_json()["sent"] == 1
    logs = client.get("/api/reminders", headers=admin).get_json()
    assert len(logs) == 1 and logs[0]["recipient"] == "remind@example.com"
