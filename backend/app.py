import os, secrets
from dotenv import load_dotenv
load_dotenv()  # Load backend/.env before config.py reads environment variables.
from datetime import datetime, date, time, timezone
from functools import wraps
from flask import Flask, jsonify, request, g
from flask_cors import CORS
from flask_security import Security, SQLAlchemyUserDatastore, hash_password, verify_password, auth_required, current_user, roles_required
from sqlalchemy.exc import IntegrityError
from config import Config
from models import db, User, Role, Department, Doctor, Patient, Appointment, DoctorAvailability, Treatment, ReminderLog
from reminders import send_due_reminders

security = Security()

def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    def is_placeholder_secret(value):
        normalized = str(value or "").strip().lower()
        return not normalized or any(marker in normalized for marker in ("replace-with", "changethis", "placeholder", "your-secret", "example-secret"))
    if is_placeholder_secret(app.config.get("SECRET_KEY")) or len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("Set SECRET_KEY to a unique random value of at least 32 characters in backend/.env")
    if is_placeholder_secret(app.config.get("SECURITY_PASSWORD_SALT")) or len(app.config["SECURITY_PASSWORD_SALT"]) < 32:
        raise RuntimeError("Set SECURITY_PASSWORD_SALT to a unique random value of at least 32 characters in backend/.env")
    CORS(app, resources={r"/api/*": {"origins": os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")}})
    db.init_app(app)
    datastore = SQLAlchemyUserDatastore(db, User, Role)
    security.init_app(app, datastore)
    app.extensions["user_datastore"] = datastore

    # API clients must always receive JSON status codes, never HTML redirects.
    @security.unauthn_handler
    def unauthenticated(mechanisms=None, headers=None):
        return jsonify(error="unauthorized", message="Authentication token missing or invalid"), 401

    @security.unauthz_handler
    def unauthorized(func_name=None, params=None):
        return jsonify(error="forbidden", message="You do not have permission to perform this action"), 403

    @app.errorhandler(404)
    def not_found(_): return jsonify(error="not_found", message="Resource not found"), 404
    @app.errorhandler(400)
    def bad_request(_): return jsonify(error="bad_request", message="Invalid request"), 400

    def json_error(message, status=400): return jsonify(error="request_error", message=message), status
    def admin_required(fn):
        @wraps(fn)
        @auth_required("token")
        @roles_required("admin")
        def wrapped(*args, **kwargs): return fn(*args, **kwargs)
        return wrapped
    def payload():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    def serialize_department(d): return {"id": d.id, "name": d.name, "description": d.description or ""}
    def serialize_doctor(d): return {"id": d.id, "full_name": d.full_name, "specialization": d.specialization, "phone": d.phone or "", "available": d.available, "department_id": d.department_id, "department_name": d.department.name if d.department else None}
    def serialize_patient(p): return {"id": p.id, "full_name": p.full_name, "email": p.email or (p.user.email if p.user else ""), "phone": p.phone or "", "date_of_birth": p.date_of_birth.isoformat() if p.date_of_birth else None}
    def serialize_appointment(a): return {"id": a.id, "patient_id": a.patient_id, "patient_name": a.patient.full_name, "doctor_id": a.doctor_id, "doctor_name": a.doctor.full_name, "scheduled_at": a.scheduled_at.isoformat(timespec="minutes") + "Z", "reason": a.reason or "", "status": a.status, "notes": a.notes or ""}
    def parse_datetime(value):
        """Parse ISO datetime; normalize timezone-aware input to naive UTC for SQLite."""
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    def serialize_availability(slot):
        return {"id": slot.id, "doctor_id": slot.doctor_id, "date": slot.date.isoformat(), "start_time": slot.start_time.strftime("%H:%M"), "end_time": slot.end_time.strftime("%H:%M"), "is_available": slot.is_available}
    def serialize_treatment(t):
        return {"id": t.id, "appointment_id": t.appointment_id, "diagnosis": t.diagnosis, "prescription": t.prescription, "notes": t.notes, "created_at": t.created_at.isoformat(timespec="minutes")}

    @app.get("/api/health")
    def health(): return jsonify(status="ok", service="hospital-management-api")

    @app.post("/api/auth/register")
    def register():
        data = payload(); name = str(data.get("full_name", "")).strip(); email = str(data.get("email", "")).strip().lower(); password = str(data.get("password", ""))
        if not name or len(name) > 120: return json_error("full_name is required (max 120 characters)")
        if "@" not in email or len(email) > 255: return json_error("A valid email is required")
        if len(password) < 8: return json_error("Password must be at least 8 characters")
        if User.query.filter_by(email=email).first(): return json_error("Email is already registered", 409)
        datastore = app.extensions["user_datastore"]
        user = datastore.create_user(email=email, password=hash_password(password), full_name=name, fs_uniquifier=secrets.token_urlsafe(32), roles=[datastore.find_role("patient")])
        db.session.add(user); db.session.flush()
        patient = Patient(user_id=user.id, full_name=name, email=email, phone=str(data.get("phone", ""))[:30])
        db.session.add(patient)
        try: db.session.commit()
        except IntegrityError: db.session.rollback(); return json_error("Could not create account", 409)
        return jsonify(message="Registration successful. Please log in.", user={"id": user.id, "email": user.email, "full_name": user.full_name, "role": "patient"}), 201

    @app.post("/api/auth/login")
    def login():
        data = payload(); email = str(data.get("email", "")).strip().lower(); password = str(data.get("password", ""))
        user = User.query.filter_by(email=email, active=True).first()
        if not user or not verify_password(password, user.password): return json_error("Invalid email or password", 401)
        token = user.get_auth_token()
        role = user.roles[0].name if user.roles else "patient"
        return jsonify(token=token, user={"id": user.id, "email": user.email, "full_name": user.full_name, "role": role})

    @app.post("/api/auth/logout")
    @auth_required("token")
    def logout():
        # Flask-Security tokens are stateless. Rotate fs_uniquifier to invalidate all tokens for this user.
        current_user.fs_uniquifier = secrets.token_urlsafe(32); db.session.commit()
        return jsonify(message="Logged out; previously issued tokens are invalidated")

    @app.get("/api/auth/me")
    @auth_required("token")
    def me(): return jsonify(id=current_user.id, email=current_user.email, full_name=current_user.full_name, roles=[r.name for r in current_user.roles])

    @app.get("/api/departments")
    def departments_list(): return jsonify([serialize_department(d) for d in Department.query.order_by(Department.name).all()])
    @app.post("/api/departments")
    @admin_required
    def departments_create():
        data=payload(); name=str(data.get("name", "")).strip()
        if not name: return json_error("name is required")
        d=Department(name=name, description=str(data.get("description", ""))); db.session.add(d)
        try: db.session.commit()
        except IntegrityError: db.session.rollback(); return json_error("Department already exists", 409)
        return jsonify(serialize_department(d)), 201
    @app.put("/api/departments/<int:item_id>")
    @admin_required
    def departments_update(item_id):
        d=db.session.get(Department,item_id)
        if not d: return json_error("Department not found",404)
        data=payload(); name=str(data.get("name",d.name)).strip()
        if not name: return json_error("name cannot be empty")
        d.name=name; d.description=str(data.get("description",d.description or ""))
        try: db.session.commit()
        except IntegrityError: db.session.rollback(); return json_error("Department name already exists",409)
        return jsonify(serialize_department(d))
    @app.delete("/api/departments/<int:item_id>")
    @admin_required
    def departments_delete(item_id):
        d=db.session.get(Department,item_id)
        if not d: return json_error("Department not found",404)
        if Doctor.query.filter_by(department_id=d.id).first(): return json_error("Move doctors to another department before deleting",409)
        db.session.delete(d); db.session.commit(); return "",204

    @app.get("/api/doctors/me")
    @auth_required("token")
    def doctors_me():
        doctor = Doctor.query.filter_by(user_id=current_user.id).first()
        if not doctor: return json_error("Doctor profile is not linked to this account", 404)
        return jsonify(serialize_doctor(doctor))

    @app.get("/api/doctors")
    def doctors_list():
        query=Doctor.query
        if request.args.get("q"): query=query.filter(Doctor.full_name.ilike(f"%{request.args['q']}%"))
        return jsonify([serialize_doctor(d) for d in query.order_by(Doctor.full_name).all()])
    @app.post("/api/doctors")
    @admin_required
    def doctors_create():
        data = payload()
        name = str(data.get("full_name", "")).strip()
        spec = str(data.get("specialization", "")).strip()
        email = str(data.get("account_email", "")).strip().lower()
        password = str(data.get("account_password", ""))
        if not name or len(name) > 120 or not spec or len(spec) > 120:
            return json_error("full_name and specialization are required (max 120 characters)")
        if bool(email) != bool(password):
            return json_error("Provide both account_email and account_password to create a doctor login")
        if email and ("@" not in email or len(email) > 255):
            return json_error("A valid account_email is required")
        if email and len(password) < 12:
            return json_error("Doctor account password must be at least 12 characters")
        if email and User.query.filter_by(email=email).first():
            return json_error("That email already has an account", 409)
        dep_id = data.get("department_id")
        if dep_id not in (None, ""):
            try: dep_id = int(dep_id)
            except (TypeError, ValueError): return json_error("department_id must be an integer")
            if not db.session.get(Department, dep_id): return json_error("Department not found", 404)
        else:
            dep_id = None
        doctor_user = None
        try:
            if email:
                datastore = app.extensions["user_datastore"]
                doctor_user = datastore.create_user(email=email, password=hash_password(password), full_name=name, fs_uniquifier=secrets.token_urlsafe(32), roles=[datastore.find_role("doctor")])
                db.session.add(doctor_user)
                db.session.flush()
            d = Doctor(full_name=name, specialization=spec, phone=str(data.get("phone", ""))[:30], department_id=dep_id, available=bool(data.get("available", True)), user_id=doctor_user.id if doctor_user else None)
            db.session.add(d)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return json_error("Could not create doctor; check for duplicate account or data", 409)
        result = serialize_doctor(d)
        result["login_account_created"] = bool(doctor_user)
        return jsonify(result), 201
    @app.put("/api/doctors/<int:item_id>")
    @admin_required
    def doctors_update(item_id):
        d=db.session.get(Doctor,item_id)
        if not d:return json_error("Doctor not found",404)
        data=payload()
        for key in ("full_name","specialization"):
            if key in data:
                value=str(data[key]).strip()
                if not value or len(value)>120:return json_error(f"{key} is required (max 120 characters)")
                setattr(d,key,value)
        if "phone" in data:d.phone=str(data["phone"]).strip()[:30]
        if "available" in data:d.available=bool(data["available"])
        if "department_id" in data:
            dep_id=data["department_id"]
            if dep_id in (None,""):dep_id=None
            else:
                try:dep_id=int(dep_id)
                except (TypeError,ValueError):return json_error("department_id must be an integer")
                if not db.session.get(Department,dep_id):return json_error("Department not found",404)
            d.department_id=dep_id
        db.session.commit();return jsonify(serialize_doctor(d))
    @app.delete("/api/doctors/<int:item_id>")
    @admin_required
    def doctors_delete(item_id):
        d=db.session.get(Doctor,item_id)
        if not d:return json_error("Doctor not found",404)
        if Appointment.query.filter_by(doctor_id=d.id).first():return json_error("Cannot delete a doctor with appointment history",409)
        linked_user = d.user
        db.session.delete(d)
        if linked_user: db.session.delete(linked_user)
        db.session.commit();return "",204

    @app.get("/api/doctors/<int:item_id>/availability")
    def doctors_availability_list(item_id):
        if not db.session.get(Doctor, item_id): return json_error("Doctor not found", 404)
        query = DoctorAvailability.query.filter_by(doctor_id=item_id)
        if request.args.get("from"):
            try: query = query.filter(DoctorAvailability.date >= date.fromisoformat(request.args["from"]))
            except ValueError: return json_error("from must be YYYY-MM-DD")
        if request.args.get("to"):
            try: query = query.filter(DoctorAvailability.date <= date.fromisoformat(request.args["to"]))
            except ValueError: return json_error("to must be YYYY-MM-DD")
        return jsonify([serialize_availability(s) for s in query.order_by(DoctorAvailability.date, DoctorAvailability.start_time).all()])

    @app.post("/api/doctors/<int:item_id>/availability")
    @auth_required("token")
    def doctors_availability_create(item_id):
        doctor = db.session.get(Doctor, item_id)
        if not doctor: return json_error("Doctor not found", 404)
        if not current_user.has_role("admin") and not (current_user.has_role("doctor") and doctor.user_id == current_user.id):
            return json_error("Only this doctor or an admin can manage availability", 403)
        data = payload()
        try:
            slot_date = date.fromisoformat(str(data.get("date", "")))
            start = time.fromisoformat(str(data.get("start_time", "")))
            end = time.fromisoformat(str(data.get("end_time", "")))
        except ValueError: return json_error("date, start_time and end_time must be valid ISO date/time values")
        if slot_date < date.today(): return json_error("Availability date must be today or in the future")
        if start >= end: return json_error("end_time must be later than start_time")
        overlap = DoctorAvailability.query.filter_by(doctor_id=item_id, date=slot_date, is_available=True).filter(DoctorAvailability.start_time < end, DoctorAvailability.end_time > start).first()
        if overlap: return json_error("Availability windows cannot overlap", 409)
        slot = DoctorAvailability(doctor_id=item_id, date=slot_date, start_time=start, end_time=end, is_available=bool(data.get("is_available", True)))
        db.session.add(slot)
        try: db.session.commit()
        except IntegrityError: db.session.rollback(); return json_error("Availability slot already exists", 409)
        return jsonify(serialize_availability(slot)), 201

    @app.delete("/api/doctors/<int:item_id>/availability/<int:slot_id>")
    @auth_required("token")
    def doctors_availability_delete(item_id, slot_id):
        doctor = db.session.get(Doctor, item_id)
        slot = db.session.get(DoctorAvailability, slot_id)
        if not doctor or not slot or slot.doctor_id != item_id: return json_error("Availability slot not found", 404)
        if not current_user.has_role("admin") and not (current_user.has_role("doctor") and doctor.user_id == current_user.id): return json_error("Forbidden", 403)
        db.session.delete(slot); db.session.commit(); return "", 204

    @app.get("/api/patients/me")
    @auth_required("token")
    def patient_me():
        patient = Patient.query.filter_by(user_id=current_user.id).first()
        if not patient: return json_error("Patient profile not found", 404)
        return jsonify(serialize_patient(patient))

    @app.put("/api/patients/me")
    @auth_required("token")
    def patient_me_update():
        patient = Patient.query.filter_by(user_id=current_user.id).first()
        if not patient: return json_error("Patient profile not found", 404)
        data = payload()
        if "full_name" in data:
            name = str(data["full_name"]).strip()
            if not name or len(name) > 120: return json_error("full_name is required (max 120 characters)")
            patient.full_name = name; current_user.full_name = name
        if "phone" in data: patient.phone = str(data["phone"]).strip()[:30]
        if "date_of_birth" in data:
            try: patient.date_of_birth = date.fromisoformat(data["date_of_birth"]) if data["date_of_birth"] else None
            except (TypeError, ValueError): return json_error("date_of_birth must be YYYY-MM-DD")
        db.session.commit(); return jsonify(serialize_patient(patient))

    @app.get("/api/patients")
    @admin_required
    def patients_list(): return jsonify([serialize_patient(p) for p in Patient.query.order_by(Patient.full_name).all()])
    @app.put("/api/patients/<int:item_id>")
    @admin_required
    def patients_update(item_id):
        p=db.session.get(Patient,item_id)
        if not p:return json_error("Patient not found",404)
        data=payload()
        for key in ("full_name","phone","email"):
            if key in data:setattr(p,key,str(data[key]).strip())
        db.session.commit();return jsonify(serialize_patient(p))
    @app.delete("/api/patients/<int:item_id>")
    @admin_required
    def patients_delete(item_id):
        p=db.session.get(Patient,item_id)
        if not p:return json_error("Patient not found",404)
        if Appointment.query.filter_by(patient_id=p.id).first():return json_error("Cannot delete a patient with appointment history",409)
        if p.user: db.session.delete(p.user)
        db.session.delete(p);db.session.commit();return "",204

    @app.get("/api/appointments")
    @auth_required("token")
    def appointments_list():
        query=Appointment.query
        if not current_user.has_role("admin"):
            patient=Patient.query.filter_by(user_id=current_user.id).first()
            doctor=Doctor.query.filter_by(user_id=current_user.id).first()
            if patient: query=query.filter_by(patient_id=patient.id)
            elif doctor: query=query.filter_by(doctor_id=doctor.id)
            else: return jsonify([])
        return jsonify([serialize_appointment(a) for a in query.order_by(Appointment.scheduled_at.desc()).all()])
    @app.post("/api/appointments")
    @auth_required("token")
    def appointments_create():
        data=payload()
        try: patient_id=int(data.get("patient_id")) if current_user.has_role("admin") else Patient.query.filter_by(user_id=current_user.id).one().id
        except Exception:return json_error("Patient profile not found",403)
        if not db.session.get(Patient, patient_id): return json_error("Patient not found", 404)
        try: doctor_id=int(data.get("doctor_id")); scheduled=parse_datetime(data.get("scheduled_at", ""))
        except (TypeError,ValueError):return json_error("doctor_id and ISO scheduled_at are required")
        doctor=db.session.get(Doctor,doctor_id)
        if not doctor or not doctor.available:return json_error("Doctor is unavailable",409)
        if scheduled <= datetime.utcnow():return json_error("Appointment must be in the future")
        conflict=Appointment.query.filter_by(doctor_id=doctor_id,scheduled_at=scheduled).filter(Appointment.status=="scheduled").first()
        if conflict:return json_error("Doctor already has an appointment at that time",409)
        configured_slots=DoctorAvailability.query.filter_by(doctor_id=doctor_id,date=scheduled.date()).all()
        if configured_slots and not any(slot.is_available and slot.start_time <= scheduled.time() < slot.end_time for slot in configured_slots):
            return json_error("Appointment time is outside the doctor's available hours",409)
        a=Appointment(patient_id=patient_id,doctor_id=doctor_id,scheduled_at=scheduled,reason=str(data.get("reason",""))[:500]);db.session.add(a);db.session.commit();return jsonify(serialize_appointment(a)),201
    @app.put("/api/appointments/<int:item_id>")
    @auth_required("token")
    def appointments_update(item_id):
        a=db.session.get(Appointment,item_id)
        if not a:return json_error("Appointment not found",404)
        if not current_user.has_role("admin"):
            patient=Patient.query.filter_by(user_id=current_user.id).first(); doctor=Doctor.query.filter_by(user_id=current_user.id).first()
            if not ((patient and a.patient_id==patient.id) or (doctor and a.doctor_id==doctor.id)):return json_error("Forbidden",403)
        data=payload()
        if "status" in data:
            if data["status"] not in {"scheduled","completed","cancelled"}:return json_error("Invalid status")
            if not current_user.has_role("admin"):
                if data["status"] == "completed" and not current_user.has_role("doctor"):
                    return json_error("Only doctors or admins can mark appointments completed", 403)
                if data["status"] == "cancelled" and not current_user.has_role("doctor") and not current_user.has_role("patient"):
                    return json_error("Forbidden", 403)
            a.status=data["status"]
        if "notes" in data and (current_user.has_role("admin") or current_user.has_role("doctor")):a.notes=str(data["notes"])
        if "scheduled_at" in data:
            if a.status != "scheduled": return json_error("Only scheduled appointments can be rescheduled", 409)
            if not (current_user.has_role("admin") or current_user.has_role("patient")):
                return json_error("Only the patient or an admin can reschedule an appointment", 403)
            try: scheduled = parse_datetime(data["scheduled_at"])
            except (TypeError, ValueError): return json_error("scheduled_at must be a valid ISO datetime")
            if scheduled <= datetime.utcnow(): return json_error("Appointment must be in the future")
            conflict = Appointment.query.filter(Appointment.id != a.id, Appointment.doctor_id == a.doctor_id, Appointment.scheduled_at == scheduled, Appointment.status == "scheduled").first()
            if conflict: return json_error("Doctor already has an appointment at that time", 409)
            configured_slots = DoctorAvailability.query.filter_by(doctor_id=a.doctor_id, date=scheduled.date()).all()
            if configured_slots and not any(slot.is_available and slot.start_time <= scheduled.time() < slot.end_time for slot in configured_slots):
                return json_error("Appointment time is outside the doctor's available hours", 409)
            a.scheduled_at = scheduled
        if "reason" in data and (current_user.has_role("admin") or current_user.has_role("patient")):a.reason=str(data["reason"])[:500]
        db.session.commit();return jsonify(serialize_appointment(a))
    @app.delete("/api/appointments/<int:item_id>")
    @auth_required("token")
    def appointments_delete(item_id):
        a=db.session.get(Appointment,item_id)
        if not a:return json_error("Appointment not found",404)
        patient=Patient.query.filter_by(user_id=current_user.id).first()
        if not current_user.has_role("admin") and not (patient and a.patient_id==patient.id):return json_error("Forbidden",403)
        if a.status!="scheduled":return json_error("Only scheduled appointments can be cancelled",409)
        a.status="cancelled";db.session.commit();return jsonify(message="Appointment cancelled")

    @app.get("/api/appointments/<int:item_id>/treatments")
    @auth_required("token")
    def treatments_list(item_id):
        appointment = db.session.get(Appointment, item_id)
        if not appointment: return json_error("Appointment not found", 404)
        patient = Patient.query.filter_by(user_id=current_user.id).first()
        doctor = Doctor.query.filter_by(user_id=current_user.id).first()
        allowed = current_user.has_role("admin") or (patient and appointment.patient_id == patient.id) or (doctor and appointment.doctor_id == doctor.id)
        if not allowed: return json_error("Forbidden", 403)
        return jsonify([serialize_treatment(t) for t in Treatment.query.filter_by(appointment_id=item_id).order_by(Treatment.created_at).all()])

    @app.post("/api/appointments/<int:item_id>/treatments")
    @auth_required("token")
    def treatments_create(item_id):
        appointment = db.session.get(Appointment, item_id)
        if not appointment: return json_error("Appointment not found", 404)
        doctor = Doctor.query.filter_by(user_id=current_user.id).first()
        if not (current_user.has_role("admin") or (doctor and appointment.doctor_id == doctor.id)):
            return json_error("Only the assigned doctor or an admin can record treatment", 403)
        if appointment.status not in {"scheduled", "completed"}: return json_error("Treatment can only be recorded for an active or completed appointment", 409)
        data = payload()
        diagnosis = str(data.get("diagnosis", "")).strip()
        prescription = str(data.get("prescription", "")).strip()
        notes = str(data.get("notes", "")).strip()
        if not diagnosis: return json_error("diagnosis is required")
        if len(diagnosis) > 1000 or len(prescription) > 2000: return json_error("Diagnosis or prescription is too long")
        treatment = Treatment(appointment_id=item_id, diagnosis=diagnosis, prescription=prescription, notes=notes)
        appointment.status = "completed"
        db.session.add(treatment); db.session.commit()
        return jsonify(serialize_treatment(treatment)), 201

    @app.post("/api/reminders/run")
    @admin_required
    def reminders_run():
        """Send due appointment reminders now (the same job Celery beat runs on a schedule)."""
        sent = send_due_reminders()
        return jsonify(sent=sent, message=f"{sent} reminder(s) sent")

    @app.get("/api/reminders")
    @admin_required
    def reminders_list():
        logs = ReminderLog.query.order_by(ReminderLog.sent_at.desc()).limit(100).all()
        return jsonify([{"id": r.id, "appointment_id": r.appointment_id, "recipient": r.recipient, "channel": r.channel, "sent_at": r.sent_at.isoformat(timespec="seconds") + "Z"} for r in logs])

    with app.app_context():
        db.create_all()
        datastore=app.extensions["user_datastore"]
        for role_name,desc in [("admin","Hospital administrator"),("doctor","Medical doctor"),("patient","Patient")]:
            if not datastore.find_role(role_name):datastore.create_role(name=role_name,description=desc)
        db.session.commit()
        admin_email=app.config.get("ADMIN_EMAIL", os.getenv("ADMIN_EMAIL", "admin@example.com")).lower()
        if not User.query.filter_by(email=admin_email).first():
            admin_password = app.config.get("ADMIN_PASSWORD", os.getenv("ADMIN_PASSWORD", ""))
            if len(admin_password) < 12 or is_placeholder_secret(admin_password):
                raise RuntimeError("Set ADMIN_PASSWORD to a unique, non-placeholder password of at least 12 characters in backend/.env")
            admin=datastore.create_user(email=admin_email,password=hash_password(admin_password),full_name="Hospital Administrator",fs_uniquifier=secrets.token_urlsafe(32),roles=[datastore.find_role("admin")])
            db.session.add(admin);db.session.commit()
        if Department.query.count()==0:
            for name,desc in [("Cardiology","Heart and cardiovascular care"),("Pediatrics","Care for infants and children"),("Orthopedics","Bone and joint care"),("General Medicine","Primary and general care")]:db.session.add(Department(name=name,description=desc))
            db.session.commit()
    return app

app = None
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    app=create_app()
    app.run(debug=os.getenv("FLASK_DEBUG","false").lower()=="true")
