"""Optional Celery worker for background jobs (requires Redis).

Run from the backend directory:
    celery -A celery_app.celery worker --loglevel=info
    celery -A celery_app.celery beat   --loglevel=info
"""
from celery import Celery

from app import create_app
from reminders import send_due_reminders

flask_app = create_app()

celery = Celery(
    "carepoint",
    broker=flask_app.config["CELERY_BROKER_URL"],
    backend=flask_app.config["CELERY_RESULT_BACKEND"],
)
celery.conf.update(
    timezone="UTC",
    beat_schedule={
        "send-appointment-reminders": {
            "task": "carepoint.send_due_reminders",
            "schedule": flask_app.config["REMINDER_CHECK_MINUTES"] * 60.0,
        }
    },
)


class ContextTask(celery.Task):
    """Run every task inside the Flask application context."""

    def __call__(self, *args, **kwargs):
        with flask_app.app_context():
            return self.run(*args, **kwargs)


celery.Task = ContextTask


@celery.task(name="carepoint.send_due_reminders")
def send_due_reminders_task():
    return send_due_reminders()
