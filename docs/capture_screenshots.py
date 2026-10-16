"""Capture the README screenshots with Playwright.

Prerequisites: backend running on :5000, frontend running on :5173, demo doctors
seeded (python seed_demo.py), and:
    pip install playwright && playwright install chromium

Usage (from the project root):
    ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD='your-admin-password' python docs/capture_screenshots.py
"""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
EMAIL = os.environ["ADMIN_EMAIL"]
PASSWORD = os.environ["ADMIN_PASSWORD"]
OUT = Path(__file__).parent / "screenshots"
OUT.mkdir(exist_ok=True)


def go(page, label):
    page.locator("nav button", has_text=label).first.click()
    page.wait_for_timeout(700)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(URL)
    page.wait_for_selector("text=Sign in to your workspace")
    page.screenshot(path=OUT / "01-login.png")

    page.fill("input[type=email]", EMAIL)
    page.fill("input[type=password]", PASSWORD)
    page.click("text=Sign in securely")
    page.wait_for_selector("nav")
    page.wait_for_timeout(1000)
    page.screenshot(path=OUT / "02-overview.png")

    go(page, "Doctors")
    page.screenshot(path=OUT / "03-doctors.png")
    page.locator("button[title='Edit doctor']").first.click()
    page.wait_for_timeout(400)
    page.screenshot(path=OUT / "04-edit-doctor.png")

    go(page, "Departments")
    page.locator("text=Edit department").first.click()
    page.wait_for_timeout(400)
    page.screenshot(path=OUT / "05-edit-department.png")

    go(page, "Appointments")
    page.screenshot(path=OUT / "06-appointments.png")
    browser.close()
print("Saved screenshots to", OUT)
