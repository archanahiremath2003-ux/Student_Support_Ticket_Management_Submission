"""Sign-in and user management rules."""
import re

from models import db, User, Department, STUDENT, STAFF, ADMIN, ROLE_LABELS, PERMISSIONS
from workflow import WorkflowError, PermissionDenied, release_tickets

USERNAME_RE = re.compile(r"^[a-z0-9._]{3,30}$")
MIN_PASSWORD = 8


def _clean_username(raw):
    username = (raw or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise WorkflowError("Username must be 3 to 30 characters: letters, numbers, dot or underscore.")
    return username


def _check_password(password, confirm):
    if len(password or "") < MIN_PASSWORD:
        raise WorkflowError(f"Password must be at least {MIN_PASSWORD} characters.")
    if password != confirm:
        raise WorkflowError("The two passwords do not match.")


def _required(text, what):
    text = (text or "").strip()
    if not text:
        raise WorkflowError(f"{what} is required.")
    return text


def authenticate(username, password, role):
    user = User.query.filter_by(username=(username or "").strip().lower()).first()
    if user is None or not user.check_password(password or ""):
        raise WorkflowError("Wrong username or password.")
    if role and user.role != role:
        raise WorkflowError(f"This is not a {ROLE_LABELS[role].lower()} account. Use the "
                            f"{ROLE_LABELS[user.role].lower()} login.")
    if not user.is_active:
        raise WorkflowError("This account is disabled. Please contact the admin.")
    return user


def register_student(form):
    """Self sign-up. Always creates a student account, never staff or admin."""
    username = _clean_username(form.get("username"))
    if User.query.filter_by(username=username).first():
        raise WorkflowError(f"The username {username} is already taken.")
    _check_password(form.get("password"), form.get("confirm"))
    user = User(role=STUDENT, username=username)
    _apply_profile(user, form)
    user.set_password(form.get("password"))
    db.session.add(user)
    db.session.commit()
    return user


def _need_admin(actor):
    if actor.role != ADMIN:
        raise PermissionDenied("Only the admin can manage users.")


def _apply_profile(user, form):
    """Fields that can be set both when adding and when editing a user."""
    user.name = _required(form.get("name"), "Name")[:120]
    if user.role == STAFF:
        dept_id = form.get("department_id")
        dept = db.session.get(Department, int(dept_id)) if str(dept_id or "").isdigit() else None
        if dept is None:
            raise WorkflowError("Choose a department for the staff member.")
        user.department = dept
        user.department_id = dept.id
        for key, _ in PERMISSIONS:
            setattr(user, key, form.get(key) == "1")
    if user.role == STUDENT:
        roll = _required(form.get("roll_no"), "Roll number").upper()[:30]
        clash = User.query.filter(User.roll_no == roll, User.id != (user.id or 0)).first()
        if clash:
            raise WorkflowError(f"Roll number {roll} already belongs to {clash.name}.")
        user.roll_no = roll
        user.program = (form.get("program") or "").strip()[:60] or None


def create_user(actor, form):
    _need_admin(actor)
    role = form.get("role")
    if role not in (STUDENT, STAFF, ADMIN):
        raise WorkflowError("Choose a role.")
    username = _clean_username(form.get("username"))
    if User.query.filter_by(username=username).first():
        raise WorkflowError(f"The username {username} is already taken.")
    _check_password(form.get("password"), form.get("confirm"))
    user = User(role=role, username=username)
    _apply_profile(user, form)
    user.set_password(form.get("password"))
    db.session.add(user)
    db.session.commit()
    return user


def update_user(actor, user, form, now):
    """Edit details, rights and status. Returns a short summary of side effects."""
    _need_admin(actor)
    old_dept = user.department_id
    _apply_profile(user, form)
    status = form.get("status", "active")
    if status not in ("active", "leave", "disabled") or (status == "leave" and user.role != STAFF):
        raise WorkflowError("Choose a valid status.")
    if status == "disabled" and user.id == actor.id:
        raise WorkflowError("You cannot disable your own account.")
    if user.role == ADMIN and status == "disabled" and user.is_active:
        others = User.query.filter(User.role == ADMIN, User.is_active.is_(True), User.id != user.id).count()
        if others == 0:
            raise WorkflowError("At least one admin must stay active.")

    was_working = user.is_active and user.available
    if status == "disabled" and user.is_active:
        user.session_version += 1  # signs them out
    user.is_active = status != "disabled"
    user.available = status == "active"

    moved = 0
    if user.role == STAFF:
        if was_working and not (user.is_active and user.available):
            why = "Staff member disabled." if status == "disabled" else "Staff member on leave."
            moved = release_tickets(user, actor, now, why)
        elif old_dept and user.department_id != old_dept:
            moved = release_tickets(user, actor, now, "Staff member moved to another department.")
    db.session.commit()
    return moved


def reset_password(actor, user, password, confirm):
    _need_admin(actor)
    _check_password(password, confirm)
    user.set_password(password)
    user.session_version += 1
    db.session.commit()


def change_own_password(user, current, password, confirm):
    if not user.check_password(current or ""):
        raise WorkflowError("Your current password is wrong.")
    _check_password(password, confirm)
    if password == current:
        raise WorkflowError("Choose a password different from the current one.")
    user.set_password(password)
    user.session_version += 1
    db.session.commit()
