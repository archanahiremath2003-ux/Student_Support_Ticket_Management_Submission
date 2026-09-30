
from datetime import date

from models import (
    db, Ticket, TicketEvent, User, Category, Department, SlaPolicy, Holiday,
    STUDENT, STAFF, ADMIN,
    NEW, ASSIGNED, IN_PROGRESS, WAITING_STUDENT, WAITING_OTHER, RESOLVED,
    CLOSED, REJECTED, CANCELLED, CLOCK_RUNNING, PENDING, OPEN_STATUSES, ACTIVE_WORK,
    TERMINAL, PRIORITIES, STATUS_LABELS, PRIORITY_LABELS,
)
from sla import BusinessCalendar, to_ist

# Rules (office minutes; 480 = one office day, 9:30 to 5:30)
AT_RISK = 0.75
CRITICAL = 1.5
UNASSIGNED_LIMIT = 120        # 2 office hours in the queue, then escalate
REMIND_STUDENT = 960          # 2 office days waiting for the student
NO_REPLY_RESOLVE = 2400       # 5 office days: resolve automatically
AUTO_CLOSE = 1440             # 3 office days after resolution
MAX_HOLD_DAYS = 30

RESOLUTION_TYPES = ["Completed", "Information provided", "Issued or handed over"]


SUGGESTED_REPLIES = {
    "any": [
        "We have received your request and are working on it.",
        "Please visit the office counter between 10:00 and 1:00 with your ID card.",
    ],
    "fees": ["Please share the transaction ID (UTR) and the payment date.",
             "Your payment is now updated. Please check the portal."],
    "attendance": ["Please submit the leave or OD letter signed by your class teacher.",
                   "Your attendance has been corrected. Please check the portal."],
    "id_card": ["Your ID card is ready. Collect it from the Admin Office counter.",
                "Please upload a passport-size photo and your fee receipt."],
    "documents": ["Please bring the original documents for verification.",
                  "Your documents are ready for collection."],
    "certificates": ["Your certificate is ready. Collect it from the Exam Cell.",
                     "We have applied to the university and will update you when it arrives."],
}

ALLOWED = {
    NEW: {ASSIGNED, REJECTED, CANCELLED},
    ASSIGNED: {IN_PROGRESS, WAITING_STUDENT, WAITING_OTHER, RESOLVED, REJECTED, CANCELLED, NEW},
    IN_PROGRESS: {ASSIGNED, WAITING_STUDENT, WAITING_OTHER, RESOLVED, REJECTED, CANCELLED, NEW},
    WAITING_STUDENT: {IN_PROGRESS, RESOLVED, REJECTED, CANCELLED, NEW},
    WAITING_OTHER: {IN_PROGRESS, RESOLVED, REJECTED, CANCELLED, NEW},
    RESOLVED: {CLOSED, IN_PROGRESS, NEW},
    CLOSED: set(), REJECTED: set(), CANCELLED: set(),
}


class WorkflowError(Exception):
    """A rule was broken. The message is shown to the user."""


class ConflictError(WorkflowError):
    pass


class PermissionDenied(WorkflowError):
    pass


# ---- Small helpers ------------------------------------------------------------
def calendar():
    return BusinessCalendar(h.day for h in Holiday.query.all())


def today_ist(now):
    return to_ist(now).date()


def _required(text, what):
    text = (text or "").strip()
    if not text:
        raise WorkflowError(f"{what} is required.")
    return text


def _log(t, actor, kind, at, note=None, frm=None, to=None, internal=False):
    db.session.add(TicketEvent(ticket=t, actor=actor, kind=kind, created_at=at, note=note,
                               from_value=frm, to_value=to, is_internal=internal))


def _check_version(t, version):
    if version is not None and str(version) != str(t.version):
        raise ConflictError("Someone else updated this ticket while you had it open. "
                            "Your change was not saved. Check the latest version and try again.")


def _save(t):
    t.version += 1
    db.session.commit()


def _set_status(t, new, at, actor, cal, note=None):
    old = t.status
    if new not in ALLOWED[old]:
        raise WorkflowError(f"A ticket that is {STATUS_LABELS[old].lower()} cannot become "
                            f"{STATUS_LABELS[new].lower()}.")
   
    if old in CLOCK_RUNNING and new not in CLOCK_RUNNING:
        t.paused_since = at
    elif old not in CLOCK_RUNNING and new in CLOCK_RUNNING and t.paused_since:
        t.paused_minutes += cal.minutes_between(t.paused_since, at)
        t.paused_since = None
    t.status = new
    t.status_changed_at = at
    t.reminder_sent_at = None
    if new != WAITING_OTHER:
        t.hold_reason, t.hold_until = None, None
    _log(t, actor, "status", at, note=note, frm=old, to=new)


def _mark_response(t, actor, at):
    """First reply from the office that the student can see."""
    if actor is not None and actor.role != STUDENT and t.first_response_at is None:
        t.first_response_at = at


def workload(user):
    return Ticket.query.filter(Ticket.assignee_id == user.id,
                               Ticket.status.in_(ACTIVE_WORK)).count()



def can_view(user, t):
    if user.role == STUDENT:
        return t.student_id == user.id
    if user.role == ADMIN:
        return True
    return t.assignee_id == user.id or t.department_id == user.department_id


def allowed_actions(user, t):
    """The set of actions this user may take on this ticket right now."""
    s, acts = t.status, set()
    if user.role == STUDENT:
        if t.student_id == user.id:
            if s not in TERMINAL:
                acts.add("comment")
            if s == RESOLVED:
                acts |= {"confirm", "reopen"}
            elif s in PENDING:
                acts.add("cancel")
        return acts
    if not can_view(user, t):
        return acts

    admin = user.role == ADMIN
    owner = t.assignee_id == user.id
    same_dept = user.department_id == t.department_id
    if s not in TERMINAL:
        acts |= {"comment", "note"}
    if s == NEW and user.role == STAFF and user.can_take and same_dept:
        acts.add("take")
    if s in PENDING and (admin or (user.can_assign and same_dept)):
        acts.add("assign")
    if admin or owner:
        if s == ASSIGNED:
            acts.add("start")
        if s in (ASSIGNED, IN_PROGRESS):
            acts |= {"request_info", "hold"}
        if s in (WAITING_STUDENT, WAITING_OTHER):
            acts.add("resume")
        if s in ACTIVE_WORK:
            acts.add("resolve")
    if s in PENDING and (admin or (owner and user.can_reject)):
        acts.add("reject")
    if s in PENDING and (admin or (owner and user.can_priority)):
        acts.add("priority")
    if s in (NEW, ASSIGNED, IN_PROGRESS) and (admin or (owner and user.can_transfer)):
        acts.add("transfer")
    if admin and s not in TERMINAL and t.escalation_level > t.escalation_acked_level:
        acts.add("acknowledge")
    return acts


ACTION_RIGHTS = {"take": "can_take", "assign": "can_assign", "transfer": "can_transfer",
                 "reject": "can_reject", "priority": "can_priority"}


def _check(user, t, action):
    if action in allowed_actions(user, t):
        return
    right = ACTION_RIGHTS.get(action)
    if user.role == STAFF and right and not getattr(user, right):
        raise PermissionDenied("You do not have access for this. Ask the admin to give you the right.")
    if not can_view(user, t) or user.role == STUDENT or (user.role == STAFF and t.assignee_id != user.id):
        raise PermissionDenied("You are not allowed to do this on this ticket.")
    raise WorkflowError(f"This is not possible while the ticket is {t.status_label.lower()}.")



def _pick_staff(department_id, exclude_id=None):
    """Least busy staff member who is active and not on leave."""
    staff = User.query.filter_by(role=STAFF, department_id=department_id, available=True,
                                 is_active=True).order_by(User.id).all()
    staff = [u for u in staff if u.id != exclude_id]
    return min(staff, key=lambda u: (workload(u), u.id)) if staff else None


def auto_assign(t, now, cal):

    staff = _pick_staff(t.department.id)
    if staff is None:
        _log(t, None, "note", now, internal=True,
             note=f"Nobody is available in {t.department.name}. Waiting in the department queue.")
        return None
    t.assignee = staff
    _set_status(t, ASSIGNED, now, None, cal)
    _log(t, None, "assigned", now, to=staff.name, internal=True,
         note="Assigned automatically to the least busy staff member.")
    return staff


def create_ticket(student, category_id, subcategory, subject, description, now):
    if student.role != STUDENT:
        raise PermissionDenied("Only students can raise tickets.")
    category = db.session.get(Category, int(category_id)) if str(category_id or "").isdigit() else None
    if category is None:
        raise WorkflowError("Choose what the request is about.")
    subject = _required(subject, "A short title")[:160]
    description = _required(description, "Details")[:4000]
    if subcategory and subcategory not in category.subtype_list:
        raise WorkflowError("Choose a request type from the list.")
    policy = db.session.get(SlaPolicy, category.default_priority)
    t = Ticket(student=student, category=category, subcategory=subcategory or None,
               subject=subject, description=description, department=category.department,
               status=NEW, priority=category.default_priority,
               response_minutes=policy.response_minutes,
               resolution_minutes=policy.resolution_minutes,
               created_at=now, status_changed_at=now)
    db.session.add(t)
    db.session.flush()
    _log(t, student, "created", now, to=NEW,
         note=f"Sent to {t.department.name} with {PRIORITY_LABELS[t.priority].lower()} priority.")
    auto_assign(t, now, calendar())
    db.session.commit()
    return t


def take(user, t, now, version=None):
    _check_version(t, version)
    _check(user, t, "take")
    t.assignee = user
    _set_status(t, ASSIGNED, now, user, calendar())
    _log(t, user, "assigned", now, to=user.name, note="Took the ticket from the queue.", internal=True)
    _save(t)


def assign(user, t, owner_id, reason, now, version=None):
    _check_version(t, version)
    _check(user, t, "assign")
    owner = db.session.get(User, int(owner_id)) if str(owner_id or "").isdigit() else None
    if owner is None or owner.role != STAFF or owner.department_id != t.department_id:
        raise WorkflowError(f"Choose a staff member from {t.department.name}.")
    if not (owner.is_active and owner.available):
        raise WorkflowError(f"{owner.name} is not available right now.")
    if t.assignee_id == owner.id:
        raise WorkflowError(f"{owner.name} already has this ticket.")
    if t.assignee_id is not None:
        reason = _required(reason, "A reason for reassigning")
    old = t.assignee.name if t.assignee else None
    t.assignee = owner
    if t.status in (NEW, IN_PROGRESS):
        _set_status(t, ASSIGNED, now, user, calendar())
    _log(t, user, "assigned", now, frm=old, to=owner.name, note=(reason or "").strip() or None,
         internal=True)
    _save(t)


def release_tickets(staff, actor, now, why):
    """Staff on leave, disabled or moved department: their open tickets go to a
    colleague, or back to the department queue if nobody is free."""
    cal = calendar()
    moved = 0
    for t in Ticket.query.filter(Ticket.assignee_id == staff.id,
                                 Ticket.status.in_(ACTIVE_WORK)).all():
        other = _pick_staff(t.department_id, exclude_id=staff.id)
        if other:
            t.assignee = other
            if t.status == IN_PROGRESS:
                _set_status(t, ASSIGNED, now, actor, cal)
        else:
            t.assignee = None
            _set_status(t, NEW, now, actor, cal)
        _log(t, actor, "assigned", now, frm=staff.name, to=other.name if other else "Queue",
             note=why, internal=True)
        t.version += 1
        moved += 1
    return moved


def transfer(user, t, department_id, reason, now, version=None):
    """Move to another department. The SLA clock is NOT reset."""
    _check_version(t, version)
    _check(user, t, "transfer")
    reason = _required(reason, "A reason for the transfer")
    dept = db.session.get(Department, int(department_id)) if str(department_id or "").isdigit() else None
    if dept is None or dept.id == t.department_id:
        raise WorkflowError("Choose a different department.")
    cal = calendar()
    old = t.department.name
    if t.status != NEW:
        _set_status(t, NEW, now, user, cal)
    else:
        t.status_changed_at = now
    t.department = dept
    t.department_id = dept.id
    t.assignee = None
    t.transfer_count += 1
    _log(t, user, "transferred", now, frm=old, to=dept.name, note=reason)
    auto_assign(t, now, cal)
    _save(t)


# ---- Working on a ticket -----------------------------------------------------------
def start_work(user, t, now, version=None):
    _check_version(t, version)
    _check(user, t, "start")
    _set_status(t, IN_PROGRESS, now, user, calendar())
    _save(t)


def comment(user, t, text, internal, now, version=None):
    _check_version(t, version)
    _check(user, t, "note" if internal else "comment")
    text = _required(text, "A message")[:4000]
    _log(t, user, "comment", now, note=text, internal=bool(internal))
    if not internal:
        _mark_response(t, user, now)
    if user.role == STUDENT and t.status == WAITING_STUDENT:
        _set_status(t, IN_PROGRESS, now, user, calendar(), note="Student replied.")
    _save(t)


def request_info(user, t, text, now, version=None):
    _check_version(t, version)
    _check(user, t, "request_info")
    text = _required(text, "The question for the student")
    _set_status(t, WAITING_STUDENT, now, user, calendar())
    _log(t, user, "comment", now, note=text)
    _mark_response(t, user, now)
    _save(t)


def put_on_hold(user, t, reason, until, now, version=None):
    _check_version(t, version)
    _check(user, t, "hold")
    reason = _required(reason, "What the ticket is waiting for")[:200]
    if not until:
        raise WorkflowError("Set a follow-up date.")
    if isinstance(until, str):
        try:
            until = date.fromisoformat(until)
        except ValueError:
            raise WorkflowError("The follow-up date is not valid.")
    today = today_ist(now)
    if until < today or (until - today).days > MAX_HOLD_DAYS:
        raise WorkflowError(f"The follow-up date must be within the next {MAX_HOLD_DAYS} days.")
    _set_status(t, WAITING_OTHER, now, user, calendar())
    t.hold_reason, t.hold_until = reason, until
    _log(t, user, "comment", now, note=f"On hold: {reason}. Follow-up by {until:%d %b %Y}.")
    _mark_response(t, user, now)
    _save(t)


def resume(user, t, now, version=None):
    _check_version(t, version)
    _check(user, t, "resume")
    _set_status(t, IN_PROGRESS, now, user, calendar())
    _save(t)


def resolve(user, t, resolution_type, note, now, version=None, cal=None, auto=False):
    _check_version(t, version)
    if not auto:
        _check(user, t, "resolve")
    note = _required(note, "A note for the student")
    if not auto and resolution_type not in RESOLUTION_TYPES:
        raise WorkflowError("Choose how the request was resolved.")
    _set_status(t, RESOLVED, now, user, cal or calendar())
    t.resolved_at = now
    t.resolution_type, t.resolution_note = resolution_type, note
    _log(t, user, "resolved", now, to=resolution_type, note=note)
    _mark_response(t, user, now)
    if auto:
        t.version += 1
    else:
        _save(t)


def reject(user, t, reason, now, version=None):
    _check_version(t, version)
    _check(user, t, "reject")
    reason = _required(reason, "A reason for rejecting")
    _set_status(t, REJECTED, now, user, calendar())
    t.closed_at = now
    _log(t, user, "comment", now, note=f"Rejected: {reason}")
    _mark_response(t, user, now)
    _save(t)


def change_priority(user, t, priority, reason, now, version=None):
    _check_version(t, version)
    _check(user, t, "priority")
    if priority not in PRIORITIES:
        raise WorkflowError("Choose a valid priority.")
    if priority == t.priority:
        raise WorkflowError("The ticket already has that priority.")
    reason = _required(reason, "A reason for changing priority")
    policy = db.session.get(SlaPolicy, priority)
    old = t.priority
    t.priority = priority
    t.response_minutes, t.resolution_minutes = policy.response_minutes, policy.resolution_minutes
    _log(t, user, "priority", now, frm=old, to=priority, note=reason, internal=True)
    _save(t)


def acknowledge(user, t, note, now, version=None):
    """An escalation needs an admin to write what will happen next."""
    _check_version(t, version)
    _check(user, t, "acknowledge")
    note = _required(note, "The action plan")
    t.escalation_acked_level = t.escalation_level
    _log(t, user, "acknowledged", now, to=f"Level {t.escalation_level}", note=note, internal=True)
    _save(t)


# ---- Student actions ------------------------------------------------------------------
def confirm(user, t, now, version=None):
    _check_version(t, version)
    _check(user, t, "confirm")
    _set_status(t, CLOSED, now, user, calendar(), note="Student confirmed it is solved.")
    t.closed_at = now
    _save(t)


def reopen(user, t, reason, now, version=None):
    _check_version(t, version)
    _check(user, t, "reopen")
    reason = _required(reason, "What is still wrong")
    cal = calendar()
    t.reopen_count += 1
    t.resolved_at = None
    _log(t, user, "reopened", now, note=reason)
    owner = t.assignee
    if owner and owner.is_active and owner.available and owner.role == STAFF:
        _set_status(t, IN_PROGRESS, now, user, cal)
    else:
        t.assignee = None
        _set_status(t, NEW, now, user, cal)
        auto_assign(t, now, cal)
    _save(t)


def cancel(user, t, reason, now, version=None):
    _check_version(t, version)
    _check(user, t, "cancel")
    _set_status(t, CANCELLED, now, user, calendar(),
                note=(reason or "").strip() or "Withdrawn by the student.")
    t.closed_at = now
    _save(t)


# ---- Reading: SLA, age, whose move ------------------------------------------------------
def sla_info(t, now, cal):
    elapsed = cal.minutes_between(t.created_at, now)
    paused = t.paused_minutes + (cal.minutes_between(t.paused_since, now) if t.paused_since else 0.0)
    used = max(0.0, elapsed - paused)
    target = t.resolution_minutes
    pct = used / target if target else 0.0
    running = t.status in CLOCK_RUNNING
    if t.status in (RESOLVED, CLOSED):
        state = "met" if used <= target else "missed"
    elif t.status in (REJECTED, CANCELLED):
        state = "stopped"
    elif pct >= 1:
        state = "breached"
    elif not running:
        state = "paused"
    elif pct >= AT_RISK:
        state = "at_risk"
    else:
        state = "on_track"
    response_due = cal.add_minutes(t.created_at, t.response_minutes)
    if t.first_response_at:
        response = "met" if t.first_response_at <= response_due else "late"
    elif t.status in TERMINAL:
        response = "stopped"
    else:
        response = "overdue" if now > response_due else "pending"
    return {"used": used, "target": target, "pct": pct, "remaining": target - used,
            "state": state, "running": running, "paused": paused,
            "due": cal.add_minutes(t.created_at, target + paused) if running else None,
            "response_due": response_due, "response": response}


def age_days(t, now):
    return (now - t.created_at).total_seconds() / 86400


AGE_BUCKETS = ["0 to 1 days", "2 to 3 days", "4 to 7 days", "8+ days"]


def age_bucket(days):
    return AGE_BUCKETS[0 if days < 2 else 1 if days < 4 else 2 if days < 8 else 3]


def hold_overdue(t, now):
    return t.status == WAITING_OTHER and t.hold_until and t.hold_until < today_ist(now)


def whose_move(t, now):
    if t.status == NEW:
        return f"{t.department.name} queue"
    if t.status in (ASSIGNED, IN_PROGRESS) or hold_overdue(t, now):
        return t.assignee.name if t.assignee else "Admin"
    if t.status == WAITING_STUDENT:
        return f"Student ({t.student.name})"
    if t.status == WAITING_OTHER:
        return f"On hold: {t.hold_reason}"
    if t.status == RESOLVED:
        return "Student, to confirm"
    return "Nobody (finished)"


def needs_action_by(user, t, now):
    if user.role == STUDENT:
        return t.student_id == user.id and t.status in (WAITING_STUDENT, RESOLVED)
    if user.role == STAFF:
        return t.assignee_id == user.id and (t.status in (ASSIGNED, IN_PROGRESS) or hold_overdue(t, now))
    if t.status in TERMINAL:
        return False
    return t.status == NEW or t.escalation_level > t.escalation_acked_level


def action_reason(user, t, now):
    if user.role == STUDENT:
        return "Reply to the office" if t.status == WAITING_STUDENT else "Check the fix: confirm or reopen"
    if user.role == ADMIN:
        if t.status == NEW:
            return "Unassigned: choose a staff member"
        return f"Escalation level {t.escalation_level}: write an action plan"
    if hold_overdue(t, now):
        return "Hold follow-up date has passed"
    return "New for you: start work" if t.status == ASSIGNED else "In progress with you"


# ---- Automatic checks ----------------------------------------------------------------
def run_automations(now):
    """Safe to run any number of times. Events get the time the limit was
    actually crossed, even if the server was asleep at that moment."""
    cal = calendar()
    total = 0
    for _ in range(3):  # one ticket can cross two limits in one run
        changed = sum(_check_one(t, now, cal) for t in
                      Ticket.query.filter(Ticket.status.in_(OPEN_STATUSES)).all())
        total += changed
        if not changed:
            break
    db.session.commit()
    return total


def _check_one(t, now, cal):
    changed = 0
    if t.status == WAITING_STUDENT:
        waited = cal.minutes_between(t.status_changed_at, now)
        if waited >= NO_REPLY_RESOLVE:
            at = cal.add_minutes(t.status_changed_at, NO_REPLY_RESOLVE)
            resolve(None, t, "No reply from student",
                    "We did not hear back for 5 office days, so this was closed as resolved. "
                    "Reopen it if you still need help.", at, cal=cal, auto=True)
            return 1
        if waited >= REMIND_STUDENT and t.reminder_sent_at is None:
            at = cal.add_minutes(t.status_changed_at, REMIND_STUDENT)
            t.reminder_sent_at = at
            _log(t, None, "reminder", at, note="Reminder: the office is waiting for your reply.")
            changed += 1

    if t.status == RESOLVED:
        if cal.minutes_between(t.status_changed_at, now) >= AUTO_CLOSE:
            at = cal.add_minutes(t.status_changed_at, AUTO_CLOSE)
            _set_status(t, CLOSED, at, None, cal, note="Closed automatically 3 office days after it was resolved.")
            t.closed_at = at
            t.version += 1
            return 1
        return changed

    info = sla_info(t, now, cal)
    reasons = []  # (level, when, why)
    if info["pct"] >= 1:
        reasons.append((1, cal.add_minutes(t.created_at, t.resolution_minutes + t.paused_minutes),
                        "Resolution time limit crossed."))
    if info["response"] == "overdue":
        reasons.append((1, info["response_due"], "No first reply within the time limit."))
    if t.status == NEW and cal.minutes_between(t.status_changed_at, now) >= UNASSIGNED_LIMIT:
        reasons.append((1, cal.add_minutes(t.status_changed_at, UNASSIGNED_LIMIT),
                        "Nobody took the ticket for 2 office hours."))
    if info["pct"] >= CRITICAL:
        reasons.append((2, cal.add_minutes(t.created_at, t.resolution_minutes * CRITICAL + t.paused_minutes),
                        "Resolution time limit crossed by 50%. Marked critical."))
    for level in (1, 2):
        hits = [(when, why) for lvl, when, why in reasons if lvl == level]
        if hits and t.escalation_level < level:
            when, why = min(hits)
            t.escalation_level = level
            _log(t, None, "escalated", min(when, now), to=f"Level {level}",
                 note=f"{why} Sent to admin.", internal=True)
            t.version += 1
            changed += 1
    return changed
