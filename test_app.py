
from models import db, User, Ticket, Category, STUDENT, STAFF, ADMIN
import workflow as wf
from sla import utcnow


def login(client, username, password="password123", role=None):
    url = "/login" + (f"?as={role}" if role else "")
    return client.post(url, data={"username": username, "password": password}, follow_redirects=True)


def test_starts_with_only_the_default_admin(app):
    assert [u.username for u in User.query.all()] == ["admin"]
    assert Ticket.query.count() == 0
    c = app.test_client()
    r = login(c, "admin", "admin123", "admin")
    assert "Dashboard" in r.get_data(as_text=True)


def test_student_can_register_and_is_always_a_student(app):
    c = app.test_client()
    r = c.post("/register", follow_redirects=True, data={
        "name": "Kavya Reddy", "roll_no": "du25msc009", "program": "M.Sc", "username": "kavya",
        "password": "kavya12345", "confirm": "kavya12345", "role": "admin"})
    assert "Raise a ticket" in r.get_data(as_text=True)
    kavya = User.query.filter_by(username="kavya").one()
    assert kavya.role == STUDENT and kavya.roll_no == "DU25MSC009"


def test_register_refuses_duplicates_and_bad_input(w, app):
    c = app.test_client()
    base = {"name": "X", "program": "", "password": "abcdefgh1", "confirm": "abcdefgh1"}
    r = c.post("/register", follow_redirects=True, data={**base, "username": "arjun", "roll_no": "R77"})
    assert "already taken" in r.get_data(as_text=True)
    r = c.post("/register", follow_redirects=True, data={**base, "username": "newkid", "roll_no": "r1"})
    assert "already belongs to Arjun" in r.get_data(as_text=True)
    r = c.post("/register", follow_redirects=True, data={**base, "username": "newkid", "roll_no": "R78",
                                                          "confirm": "different1"})
    assert "do not match" in r.get_data(as_text=True)
    assert User.query.filter_by(username="newkid").count() == 0


def test_login_checks_password_role_and_active(w, app):
    c = app.test_client()
    assert "Wrong username or password" in login(c, "arjun", "nope", "student").get_data(as_text=True)
    assert "not a staff account" in login(c, "arjun", role="staff").get_data(as_text=True)
    w.arjun.is_active = False
    db.session.commit()
    assert "disabled" in login(c, "arjun", role="student").get_data(as_text=True)


def test_admin_adds_staff_with_rights(w, app):
    admin = app.test_client()
    login(admin, "principal", role="admin")
    admin.post("/admin/users/new", data={"role": "staff", "name": "Deepa", "username": "deepa",
                                         "password": "start1234", "confirm": "start1234",
                                         "department_id": w.accounts.id, "can_reject": "1"})
    deepa = User.query.filter_by(username="deepa").one()
    assert deepa.can_reject and not deepa.can_assign and not deepa.can_take
    c = app.test_client()
    login(c, "deepa", "start1234", "staff")
    assert c.get("/tickets").status_code == 200   # no forced password change


def test_duplicate_username_and_short_password_refused(w, app):
    c = app.test_client()
    login(c, "principal", role="admin")
    r = c.post("/admin/users/new", follow_redirects=True, data={
        "role": "student", "name": "X", "username": "arjun", "password": "abcdefgh",
        "confirm": "abcdefgh", "roll_no": "R9"})
    assert "already taken" in r.get_data(as_text=True)
    r = c.post("/admin/users/new", follow_redirects=True, data={
        "role": "student", "name": "X", "username": "newkid", "password": "short",
        "confirm": "short", "roll_no": "R9"})
    assert "at least 8" in r.get_data(as_text=True)


def test_admin_cannot_disable_self_or_last_admin(w, app):
    c = app.test_client()
    login(c, "principal", role="admin")
    r = c.post(f"/admin/users/{w.admin.id}", follow_redirects=True,
               data={"name": "Principal", "status": "disabled"})
    assert "cannot disable your own account" in r.get_data(as_text=True)
    assert db.session.get(User, w.admin.id).is_active


def test_password_reset_signs_the_user_out(w, app):
    student = app.test_client()
    login(student, "arjun", role="student")
    assert student.get("/tickets").status_code == 200
    admin = app.test_client()
    login(admin, "principal", role="admin")
    admin.post(f"/admin/users/{w.arjun.id}/password", data={"password": "reset1234", "confirm": "reset1234"})
    assert student.get("/tickets").status_code == 302  # back to login


def test_staff_on_leave_hands_over_tickets(w, app):
    t = wf.create_ticket(w.arjun, w.fees.id, "Refund", "Refund", "Paid twice", utcnow())
    assert t.assignee == w.ramesh
    c = app.test_client()
    login(c, "principal", role="admin")
    c.post(f"/admin/users/{w.ramesh.id}", data={"name": "Ramesh", "department_id": w.accounts.id,
                                                 "status": "leave", "can_take": "1"})
    assert db.session.get(Ticket, t.id).assignee_id == w.lakshmi.id


def test_every_page_works_and_students_see_only_their_tickets(w, app):
    t = wf.create_ticket(w.arjun, w.fees.id, "Refund", "Refund", "Paid twice", utcnow())
    wf.comment(w.ramesh, t, "internal secret", True, utcnow())
    pages = {STUDENT: ["/tickets", "/tickets/new", "/account/password"],
             STAFF: ["/tickets?tab=action", "/tickets?tab=queue", "/tickets?tab=solved"],
             ADMIN: ["/dashboard", "/tickets?tab=all", "/admin/users", "/admin/users/new?role=staff",
                     f"/admin/users/{w.ramesh.id}"]}
    for user in User.query.all():
        c = app.test_client()
        login(c, user.username, "admin123" if user.username == "admin" else "password123", user.role)
        for p in pages[user.role]:
            assert c.get(p).status_code == 200, (user.name, p)
        expected = 200 if wf.can_view(user, t) else 403
        page = c.get(f"/tickets/{t.id}")
        assert page.status_code == expected, user.name
        if user.role == STUDENT and expected == 200:
            assert "internal secret" not in page.get_data(as_text=True)


def test_form_without_csrf_token_is_refused(w):
    from app import create_app
    app2 = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                       "AUTOMATION_INTERVAL": None, "SECRET_KEY": "x"})
    assert app2.test_client().post("/login", data={"username": "a", "password": "b"}).status_code == 400


def test_student_raises_ticket_through_the_form(w, app):
    c = app.test_client()
    login(c, "arjun", role="student")
    r = c.post("/tickets/new", follow_redirects=True, data={
        "category_id": w.fees.id, "subcategory": "Refund", "subject": "Paid twice",
        "description": "Exam fee paid twice"})
    assert "sent to Accounts" in r.get_data(as_text=True)
