import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from models import db, User, Department, Category, STUDENT, STAFF, ADMIN  # noqa: E402


def ist(y, m, d, h=10, mi=0):
    """UTC time for an IST wall-clock time."""
    return datetime(y, m, d, h, mi) - timedelta(hours=5, minutes=30)


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                      "AUTOMATION_INTERVAL": None, "CSRF_ENABLED": False, "SECRET_KEY": "test"})
    with app.app_context():
        yield app
        db.session.remove()
        db.drop_all()


def make_user(name, role, dept=None, password="password123", **kw):
    u = User(name=name, username=name.lower().replace(" ", "."), role=role, department=dept, **kw)
    u.set_password(password)
    db.session.add(u)
    return u


class World:
    pass


@pytest.fixture
def w(app):
    x = World()
    x.accounts = Department.query.filter_by(name="Accounts").one()
    x.admin_office = Department.query.filter_by(name="Admin Office").one()
    x.fees = Category.query.filter_by(key="fees").one()
    x.idcard = Category.query.filter_by(key="id_card").one()
    x.admin = make_user("Principal", ADMIN)
    x.ramesh = make_user("Ramesh", STAFF, x.accounts)
    x.lakshmi = make_user("Lakshmi", STAFF, x.accounts)
    x.girish = make_user("Girish", STAFF, x.admin_office)
    x.arjun = make_user("Arjun", STUDENT, roll_no="R1")
    x.priya = make_user("Priya", STUDENT, roll_no="R2")
    db.session.commit()
    return x
