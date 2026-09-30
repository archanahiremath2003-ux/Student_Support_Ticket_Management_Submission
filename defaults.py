"""Starting configuration created on first run: departments, request
categories, time limits, holidays and the default admin account.
No students, staff or tickets are created."""
import os
from datetime import date

from models import db, Department, Category, SlaPolicy, Holiday, User, ADMIN


DEFAULT_ADMIN = ("Administrator", "admin", os.environ.get("ADMIN_PASSWORD", "admin123"))


SLA = {"urgent": (60, 480), "high": (120, 960), "medium": (240, 1440), "low": (480, 2400)}

DEPARTMENTS = ["Accounts", "Academic Section", "Admin Office", "Exam Cell"]

CATEGORIES = [  
    ("fees", "Fees", "Accounts", "high",
     "Payment not reflected|Wrong amount charged|Fee receipt needed|Installment request|Refund"),
    ("attendance", "Attendance", "Academic Section", "medium",
     "Attendance not marked|Medical leave adjustment|On-duty (OD) adjustment|Shortage query"),
    ("id_card", "ID card", "Admin Office", "low", "New card|Lost card|Correction on card"),
    ("documents", "Documents", "Admin Office", "medium",
     "Return of original documents|Document verification|Name or date of birth correction"),
    ("certificates", "Certificates", "Exam Cell", "medium",
     "Bonafide certificate|Marks card copy|Provisional certificate|Migration certificate"),
    ("other", "Other", "Admin Office", "low", ""),
]

HOLIDAYS = [(date(2026, 10, 2), "Gandhi Jayanti"), (date(2026, 11, 1), "Kannada Rajyotsava"),
            (date(2026, 12, 25), "Christmas"), (date(2027, 1, 26), "Republic Day"),
            (date(2027, 8, 15), "Independence Day")]


def load_defaults():
    if User.query.filter_by(role=ADMIN).count() == 0:
        name, username, password = DEFAULT_ADMIN
        admin = User(name=name, username=username, role=ADMIN)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
    if Department.query.count():
        return
    depts = {name: Department(name=name) for name in DEPARTMENTS}
    db.session.add_all(depts.values())
    for key, name, dept, prio, types in CATEGORIES:
        db.session.add(Category(key=key, name=name, department=depts[dept],
                                default_priority=prio, subtypes=types))
    for prio, (reply, resolve) in SLA.items():
        db.session.add(SlaPolicy(priority=prio, response_minutes=reply, resolution_minutes=resolve))
    for day, name in HOLIDAYS:
        db.session.add(Holiday(day=day, name=name))
    db.session.commit()
