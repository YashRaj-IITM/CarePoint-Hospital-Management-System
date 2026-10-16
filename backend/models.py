from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_security import UserMixin, RoleMixin

db = SQLAlchemy()
roles_users = db.Table(
    "roles_users",
    db.Column("user_id", db.Integer(), db.ForeignKey("user.id")),
    db.Column("role_id", db.Integer(), db.ForeignKey("role.id")),
)

class Role(db.Model, RoleMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    description = db.Column(db.String(255))

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    fs_uniquifier = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    roles = db.relationship("Role", secondary=roles_users, backref=db.backref("users", lazy="dynamic"))

class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.Text, default="")

class Doctor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=True)
    full_name = db.Column(db.String(120), nullable=False)
    specialization = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), default="")
    available = db.Column(db.Boolean, default=True, nullable=False)
    department = db.relationship("Department")
    user = db.relationship("User")

class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True)
    full_name = db.Column(db.String(120), nullable=False)
    date_of_birth = db.Column(db.Date, nullable=True)
    phone = db.Column(db.String(30), default="")
    email = db.Column(db.String(255), default="")
    user = db.relationship("User")

class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctor.id"), nullable=False)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    reason = db.Column(db.String(500), default="")
    status = db.Column(db.String(20), default="scheduled", nullable=False)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    patient = db.relationship("Patient")
    doctor = db.relationship("Doctor")


class DoctorAvailability(db.Model):
    """A doctor's bookable window on a specific calendar date."""
    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctor.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    is_available = db.Column(db.Boolean, default=True, nullable=False)
    doctor = db.relationship("Doctor", backref=db.backref("availability_slots", cascade="all, delete-orphan"))
    __table_args__ = (db.UniqueConstraint("doctor_id", "date", "start_time", name="uq_doctor_availability_start"),)


class Treatment(db.Model):
    """Clinical notes attached to a completed appointment."""
    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointment.id"), nullable=False, index=True)
    diagnosis = db.Column(db.String(1000), default="", nullable=False)
    prescription = db.Column(db.String(2000), default="", nullable=False)
    notes = db.Column(db.Text, default="", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    appointment = db.relationship("Appointment", backref=db.backref("treatments", cascade="all, delete-orphan"))


class ReminderLog(db.Model):
    """Records that a reminder was sent for an appointment (prevents duplicates)."""
    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointment.id"), nullable=False, unique=True, index=True)
    recipient = db.Column(db.String(255), default="", nullable=False)
    channel = db.Column(db.String(20), default="log", nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    appointment = db.relationship("Appointment", backref=db.backref("reminder", uselist=False, cascade="all, delete-orphan"))
