"""Appointment reminder logic.

Kept free of any Celery import so it can be unit-tested and also triggered
synchronously from the API. celery_app.py wraps send_due_reminders() in a
periodic Celery task.
"""
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

from flask import current_app

from models import db, Appointment, ReminderLog


def build_message(appointment):
    when = appointment.scheduled_at.strftime("%A, %d %B %Y at %H:%M UTC")
    subject = f"Reminder: appointment with {appointment.doctor.full_name}"
    body = (
        f"Hello {appointment.patient.full_name},\n\n"
        f"This is a reminder of your appointment with {appointment.doctor.full_name} "
        f"({appointment.doctor.specialization}) on {when}.\n"
        f"Reason: {appointment.reason or 'General consultation'}\n\n"
        "If you cannot attend, please cancel or reschedule from your CarePoint account.\n"
    )
    return subject, body


def recipient_for(appointment):
    patient = appointment.patient
    return patient.email or (patient.user.email if patient.user else "")


def deliver(recipient, subject, body):
    """Send by SMTP when MAIL_SERVER is configured, otherwise write to the app log.

    Returns the channel used ("email" or "log").
    """
    cfg = current_app.config
    if not cfg.get("MAIL_SERVER"):
        current_app.logger.info("REMINDER to %s | %s | %s", recipient, subject, body.replace("\n", " "))
        return "log"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["MAIL_FROM"], recipient, subject
    msg.set_content(body)
    with smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=15) as smtp:
        if cfg.get("MAIL_USE_TLS"):
            smtp.starttls()
        if cfg.get("MAIL_USERNAME"):
            smtp.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
        smtp.send_message(msg)
    return "email"


def send_due_reminders(now=None, window_hours=None):
    """Remind patients of scheduled appointments starting within the window.

    Each appointment is reminded at most once (tracked in ReminderLog).
    Returns the number of reminders sent. Must run inside an app context.
    """
    now = now or datetime.utcnow()
    hours = window_hours if window_hours is not None else current_app.config.get("REMINDER_WINDOW_HOURS", 24)
    due = (
        Appointment.query.filter(
            Appointment.status == "scheduled",
            Appointment.scheduled_at > now,
            Appointment.scheduled_at <= now + timedelta(hours=hours),
            ~Appointment.reminder.has(),
        )
        .order_by(Appointment.scheduled_at)
        .all()
    )
    sent = 0
    for appointment in due:
        recipient = recipient_for(appointment)
        if not recipient:
            continue
        subject, body = build_message(appointment)
        try:
            channel = deliver(recipient, subject, body)
        except Exception:  # one bad address/SMTP error must not block the rest
            current_app.logger.exception("Reminder failed for appointment %s", appointment.id)
            continue
        db.session.add(ReminderLog(appointment_id=appointment.id, recipient=recipient, channel=channel))
        db.session.commit()
        sent += 1
    return sent
