"""Create demo doctor records for local evaluation. Run after app.py has initialized the database."""
from dotenv import load_dotenv
load_dotenv()
from app import create_app
from models import db, Doctor, Department
app = create_app()
with app.app_context():
    if Doctor.query.count() == 0:
        departments = {d.name: d.id for d in Department.query.all()}
        examples = [
            ("Dr. Aisha Sharma", "Cardiologist", "Cardiology"),
            ("Dr. Rohan Mehta", "Pediatrician", "Pediatrics"),
            ("Dr. Neha Verma", "Orthopedic Surgeon", "Orthopedics"),
            ("Dr. Kabir Sen", "General Physician", "General Medicine"),
        ]
        for name, specialty, department in examples:
            db.session.add(Doctor(full_name=name, specialization=specialty, department_id=departments.get(department), available=True))
        db.session.commit()
        print("Demo doctors created.")
    else:
        print("Doctor records already exist; no changes made.")
