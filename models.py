from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# Roles
STUDENT, STAFF, ADMIN = "student", "staff", "admin"
ROLE_LABELS = {STUDENT: "Student", STAFF: "Staff", ADMIN: "Admin"}


PERMISSIONS = [
    ("can_take", "Take unassigned tickets from their department"),
    ("can_assign", "Assign or reassign tickets within their department"),
    ("can_transfer", "Transfer tickets to another department"),
    ("can_reject", "Reject tickets"),
    ("can_priority", "Change ticket priority"),
]

# Ticket statuses
NEW = "new"
ASSIGNED = "assigned"
IN_PROGRESS = "in_progress"
WAITING_STUDENT = "waiting_student"
WAITING_OTHER = "waiting_other"
RESOLVED = "resolved"
CLOSED = "closed"
REJECTED = "rejected"
CANCELLED = "cancelled"

STATUS_LABELS = {
    NEW: "New", ASSIGNED: "Assigned", IN_PROGRESS: "In progress",
    WAITING_STUDENT: "Waiting for student", WAITING_OTHER: "On hold",
    RESOLVED: "Resolved", CLOSED: "Closed", REJECTED: "Rejected", CANCELLED: "Withdrawn",
}

CLOCK_RUNNING = {NEW, ASSIGNED, IN_PROGRESS}          # SLA clock runs only here
PENDING = {NEW, ASSIGNED, IN_PROGRESS, WAITING_STUDENT, WAITING_OTHER}
OPEN_STATUSES = PENDING | {RESOLVED}
ACTIVE_WORK = {ASSIGNED, IN_PROGRESS, WAITING_STUDENT, WAITING_OTHER}
TERMINAL = {CLOSED, REJECTED, CANCELLED}

PRIORITIES = ["low", "medium", "high", "urgent"]
PRIORITY_LABELS = {"low": "Low", "medium": "Medium", "high": "High", "urgent": "Urgent"}


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(40), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"))
    department = db.relationship("Department")
    roll_no = db.Column(db.String(30))
    program = db.Column(db.String(60))

    is_active = db.Column(db.Boolean, default=True, nullable=False)   # False = cannot sign in
    available = db.Column(db.Boolean, default=True, nullable=False)   # False = on leave
    session_version = db.Column(db.Integer, default=1, nullable=False)  # bump to sign out everywhere

    can_take = db.Column(db.Boolean, default=True, nullable=False)
    can_assign = db.Column(db.Boolean, default=False, nullable=False)
    can_transfer = db.Column(db.Boolean, default=False, nullable=False)
    can_reject = db.Column(db.Boolean, default=False, nullable=False)
    can_priority = db.Column(db.Boolean, default=False, nullable=False)

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    def has(self, permission):
        """Admins can do everything; staff only what they were given."""
        return self.role == ADMIN or (self.role == STAFF and getattr(self, permission))


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(60), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    department = db.relationship("Department")
    default_priority = db.Column(db.String(10), nullable=False)
    subtypes = db.Column(db.String(400), default="")  # separated by |

    @property
    def subtype_list(self):
        return [s for s in (self.subtypes or "").split("|") if s]


class SlaPolicy(db.Model):
    priority = db.Column(db.String(10), primary_key=True)
    response_minutes = db.Column(db.Integer, nullable=False)    # office minutes
    resolution_minutes = db.Column(db.Integer, nullable=False)  # office minutes


class Holiday(db.Model):
    day = db.Column(db.Date, primary_key=True)
    name = db.Column(db.String(80), nullable=False)


class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    student = db.relationship("User", foreign_keys=[student_id])
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=False)
    category = db.relationship("Category")
    subcategory = db.Column(db.String(80))
    subject = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, nullable=False)

    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    department = db.relationship("Department")
    assignee_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    assignee = db.relationship("User", foreign_keys=[assignee_id])

    status = db.Column(db.String(20), nullable=False, default=NEW)
    priority = db.Column(db.String(10), nullable=False)
 

    response_minutes = db.Column(db.Integer, nullable=False)
    resolution_minutes = db.Column(db.Integer, nullable=False)

    created_at = db.Column(db.DateTime, nullable=False)
    status_changed_at = db.Column(db.DateTime, nullable=False)
    first_response_at = db.Column(db.DateTime)
    resolved_at = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)

    paused_minutes = db.Column(db.Float, nullable=False, default=0.0)
    paused_since = db.Column(db.DateTime)
    hold_reason = db.Column(db.String(200))
    hold_until = db.Column(db.Date)
    reminder_sent_at = db.Column(db.DateTime)

    resolution_type = db.Column(db.String(40))
    resolution_note = db.Column(db.Text)
    escalation_level = db.Column(db.Integer, nullable=False, default=0)
    escalation_acked_level = db.Column(db.Integer, nullable=False, default=0)
    reopen_count = db.Column(db.Integer, nullable=False, default=0)
    transfer_count = db.Column(db.Integer, nullable=False, default=0)
    version = db.Column(db.Integer, nullable=False, default=1)  # stops two people overwriting each other

    events = db.relationship("TicketEvent", order_by="TicketEvent.created_at, TicketEvent.id",
                             back_populates="ticket")

    @property
    def code(self):
        return f"TKT-{self.id:04d}"

    @property
    def status_label(self):
        return STATUS_LABELS[self.status]


class TicketEvent(db.Model):
    """Activity history. Only ever added to, never edited or deleted."""
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("ticket.id"), nullable=False)
    ticket = db.relationship("Ticket", back_populates="events")
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"))  # empty = automatic
    actor = db.relationship("User")
    kind = db.Column(db.String(30), nullable=False)
    from_value = db.Column(db.String(80))
    to_value = db.Column(db.String(80))
    note = db.Column(db.Text)
    is_internal = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False)
