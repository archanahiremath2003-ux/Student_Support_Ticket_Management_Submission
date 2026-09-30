import os
import secrets
import time
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, g
from markupsafe import Markup

from models import (db, User, Ticket, Category, Department, SlaPolicy, STUDENT, STAFF, ADMIN,
                    ROLE_LABELS, PERMISSIONS, STATUS_LABELS, PRIORITY_LABELS, PRIORITIES,
                    PENDING, NEW, RESOLVED, CLOSED, REJECTED, CANCELLED)
import workflow as wf
import accounts
import reports
from defaults import load_defaults
from sla import utcnow, to_ist, humanize_minutes, friendly

# Tabs on the tickets page, per role.
TABS = {
    STUDENT: [("pending", "Pending"), ("action", "Needs my reply"), ("solved", "Solved"),
              ("rejected", "Rejected or withdrawn"), ("all", "All")],
    STAFF: [("action", "To do"), ("pending", "My pending"), ("queue", "Unassigned"),
            ("solved", "Completed"), ("rejected", "Rejected"), ("all", "All mine")],
    ADMIN: [("action", "Needs attention"), ("pending", "Pending"), ("queue", "Unassigned"),
            ("solved", "Completed"), ("rejected", "Rejected"), ("all", "All")],
}

ACTIONS = {
    "take": lambda u, t, f, now, v: wf.take(u, t, now, v),
    "assign": lambda u, t, f, now, v: wf.assign(u, t, f.get("owner_id"), f.get("reason"), now, v),
    "start": lambda u, t, f, now, v: wf.start_work(u, t, now, v),
    "comment": lambda u, t, f, now, v: wf.comment(u, t, f.get("text"), f.get("internal") == "1", now, v),
    "request_info": lambda u, t, f, now, v: wf.request_info(u, t, f.get("text"), now, v),
    "hold": lambda u, t, f, now, v: wf.put_on_hold(u, t, f.get("reason"), f.get("until"), now, v),
    "resume": lambda u, t, f, now, v: wf.resume(u, t, now, v),
    "resolve": lambda u, t, f, now, v: wf.resolve(u, t, f.get("resolution_type"), f.get("note"), now, v),
    "reject": lambda u, t, f, now, v: wf.reject(u, t, f.get("reason"), now, v),
    "priority": lambda u, t, f, now, v: wf.change_priority(u, t, f.get("priority"), f.get("reason"), now, v),
    "transfer": lambda u, t, f, now, v: wf.transfer(u, t, f.get("department_id"), f.get("reason"), now, v),
    "acknowledge": lambda u, t, f, now, v: wf.acknowledge(u, t, f.get("note"), now, v),
    "confirm": lambda u, t, f, now, v: wf.confirm(u, t, now, v),
    "reopen": lambda u, t, f, now, v: wf.reopen(u, t, f.get("reason"), now, v),
    "cancel": lambda u, t, f, now, v: wf.cancel(u, t, f.get("reason"), now, v),
}


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "change-this-in-production"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "sqlite:///" + os.path.join(app.root_path, "helpdesk.db")),
        AUTOMATION_INTERVAL=60,   # seconds between automatic checks
        CSRF_ENABLED=True,
    )
    if test_config:
        app.config.update(test_config)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        load_defaults()
    add_template_helpers(app)
    add_routes(app)
    return app


def add_template_helpers(app):
    app.add_template_filter(lambda dt: to_ist(dt).strftime("%d %b %Y, %I:%M %p") if dt else "", "ist")
    app.add_template_filter(humanize_minutes, "dur")
    app.add_template_filter(friendly, "friendly")

    @app.template_filter("ago")
    def ago(dt, now):
        mins = (now - dt).total_seconds() / 60
        if mins < 60:
            return f"{int(mins)} min"
        if mins < 1440:
            return f"{int(mins // 60)} hours"
        days = int(mins // 1440)
        return "1 day" if days == 1 else f"{days} days"

    @app.context_processor
    def globals_for_templates():
        def csrf_field():
            if "csrf" not in session:
                session["csrf"] = secrets.token_hex(16)
            return Markup(f'<input type="hidden" name="csrf_token" value="{session["csrf"]}">')
        user = g.get("user")
        todo = 0
        if user:
            now = utcnow()
            todo = sum(wf.needs_action_by(user, t, now) for t in scoped(user))
        return {"me": user, "csrf_field": csrf_field, "todo_count": todo, "ROLE_LABELS": ROLE_LABELS,
                "STATUS_LABELS": STATUS_LABELS, "PRIORITY_LABELS": PRIORITY_LABELS}


def scoped(user):
    """Tickets this user works with."""
    q = Ticket.query
    if user.role == STUDENT:
        q = q.filter_by(student_id=user.id)
    elif user.role == STAFF:
        q = q.filter((Ticket.assignee_id == user.id) |
                     ((Ticket.department_id == user.department_id) & (Ticket.status == NEW)))
    return q.all()


def login_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            if roles and g.user.role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def add_routes(app):
    last_check = {"at": 0.0}

    @app.before_request
    def before():
        g.user = None
        uid = session.get("user_id")
        if uid:
            user = db.session.get(User, uid)
            # Signed out if disabled or the password was changed elsewhere.
            if user and user.is_active and session.get("ver") == user.session_version:
                g.user = user
            else:
                session.pop("user_id", None)
        if request.method == "POST" and app.config["CSRF_ENABLED"]:
            if not session.get("csrf") or request.form.get("csrf_token") != session.get("csrf"):
                abort(400)
        if request.endpoint == "static":
            return None
        interval = app.config["AUTOMATION_INTERVAL"]
        if interval is not None and time.time() - last_check["at"] > interval:
            last_check["at"] = time.time()
            wf.run_automations(utcnow())

    @app.errorhandler(400)
    def bad_request(_):
        return render_template("message.html", title="Form expired",
                               text="The page was open too long or was sent twice. Go back, reload and try again."), 400

    @app.errorhandler(403)
    def forbidden(_):
        return render_template("message.html", title="Not allowed",
                               text="You do not have access to this page."), 403

    @app.errorhandler(404)
    def not_found(_):
        return render_template("message.html", title="Not found",
                               text="That page or ticket does not exist."), 404

    def sign_in(user):
        session.clear()
        session["user_id"] = user.id
        session["ver"] = user.session_version

    # ---- Setup and sign-in ------------------------------------------------------
    @app.route("/register", methods=["GET", "POST"])
    def register():
        if g.user:
            return redirect(url_for("home"))
        if request.method == "POST":
            try:
                user = accounts.register_student(request.form)
                sign_in(user)
                flash("Account created. You can raise your first ticket now.", "success")
                return redirect(url_for("new_ticket"))
            except wf.WorkflowError as e:
                db.session.rollback()
                flash(str(e), "danger")
        return render_template("register.html", form=request.form)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        role = request.args.get("as", STUDENT)
        if role not in ROLE_LABELS:
            role = STUDENT
        if request.method == "POST":
            try:
                user = accounts.authenticate(request.form.get("username"),
                                             request.form.get("password"), role)
                sign_in(user)
                return redirect(url_for("home"))
            except wf.WorkflowError as e:
                flash(str(e), "danger")
        return render_template("login.html", role=role)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/account/password", methods=["GET", "POST"])
    @login_required()
    def change_password():
        if request.method == "POST":
            f = request.form
            try:
                accounts.change_own_password(g.user, f.get("current"), f.get("password"), f.get("confirm"))
                session["ver"] = g.user.session_version
                flash("Password changed.", "success")
                return redirect(url_for("home"))
            except wf.WorkflowError as e:
                flash(str(e), "danger")
        return render_template("password.html")

    @app.route("/")
    @login_required()
    def home():
        if g.user.role == ADMIN:
            return redirect(url_for("dashboard"))
        return redirect(url_for("tickets", tab="action" if g.user.role == STAFF else "pending"))

    # ---- Tickets ------------------------------------------------------------------
    @app.route("/tickets")
    @login_required()
    def tickets():
        now, cal, me = utcnow(), wf.calendar(), g.user
        tabs = TABS[me.role]
        tab = request.args.get("tab", tabs[0][0])
        if tab not in dict(tabs):
            tab = tabs[0][0]
        base = scoped(me)
        if me.role == STAFF:  # the queue is only for "Unassigned"
            mine = [t for t in base if t.assignee_id == me.id]
            queue = [t for t in base if t.status == NEW and t.department_id == me.department_id]
        else:
            mine, queue = base, [t for t in base if t.status == NEW]
        pick = {
            "action": lambda: [t for t in mine + (queue if me.role == ADMIN else []) if wf.needs_action_by(me, t, now)],
            "pending": lambda: [t for t in mine if t.status in PENDING],
            "queue": lambda: queue,
            "solved": lambda: [t for t in mine if t.status in (RESOLVED, CLOSED)],
            "rejected": lambda: [t for t in mine if t.status in (REJECTED, CANCELLED)],
            "all": lambda: mine,
        }
        counts = {key: len(set(pick[key]())) for key, _ in tabs}
        items = list({t.id: t for t in pick[tab]()}.values())

        # Admin filters
        f = {k: request.args.get(k, "") for k in ("q", "department", "category", "priority")}
        if f["q"]:
            q = f["q"].lower()
            items = [t for t in items if q in t.subject.lower() or q in t.code.lower()
                     or q in t.student.name.lower() or q in (t.student.roll_no or "").lower()]
        if f["department"]:
            items = [t for t in items if str(t.department_id) == f["department"]]
        if f["category"]:
            items = [t for t in items if str(t.category_id) == f["category"]]
        if f["priority"]:
            items = [t for t in items if t.priority == f["priority"]]

        info = {t.id: wf.sla_info(t, now, cal) for t in items}
        if tab in ("action", "pending", "queue"):
            items.sort(key=lambda t: (info[t.id]["remaining"], t.created_at))  # least time left first
        else:
            items.sort(key=lambda t: t.created_at, reverse=True)
        can_take = me.role == STAFF and me.can_take
        return render_template("tickets.html", items=items, info=info, tab=tab, tabs=tabs,
                               counts=counts, now=now, f=f, can_take=can_take,
                               reasons={t.id: wf.action_reason(me, t, now) for t in items} if tab == "action" else {},
                               move={t.id: wf.whose_move(t, now) for t in items},
                               departments=Department.query.order_by(Department.name).all(),
                               categories=Category.query.all(), priorities=PRIORITIES)

    @app.route("/tickets/new", methods=["GET", "POST"])
    @login_required(STUDENT)
    def new_ticket():
        if request.method == "POST":
            f = request.form
            try:
                t = wf.create_ticket(g.user, f.get("category_id"), f.get("subcategory"),
                                     f.get("subject"), f.get("description"), utcnow())
                flash(f"Ticket {t.code} raised and sent to {t.department.name}.", "success")
                return redirect(url_for("ticket", ticket_id=t.id))
            except wf.WorkflowError as e:
                db.session.rollback()
                flash(str(e), "danger")
        limits = {p.priority: [friendly(p.response_minutes), friendly(p.resolution_minutes)]
                  for p in SlaPolicy.query.all()}
        return render_template("new_ticket.html", categories=Category.query.all(),
                               form=request.form, limits=limits)

    @app.route("/tickets/<int:ticket_id>")
    @login_required()
    def ticket(ticket_id):
        t = db.get_or_404(Ticket, ticket_id)
        if not wf.can_view(g.user, t):
            abort(403)
        now = utcnow()
        staff_list = User.query.filter_by(role=STAFF, department_id=t.department_id,
                                          is_active=True, available=True).order_by(User.name).all()
        events = [e for e in t.events if not (e.is_internal and g.user.role == STUDENT)]
        return render_template(
            "ticket.html", t=t, events=events, now=now, info=wf.sla_info(t, now, wf.calendar()),
            acts=wf.allowed_actions(g.user, t), move=wf.whose_move(t, now),
            staff_list=staff_list, priorities=PRIORITIES, resolution_types=wf.RESOLUTION_TYPES,
            departments=Department.query.order_by(Department.name).all(),
            suggestions=wf.SUGGESTED_REPLIES["any"] + wf.SUGGESTED_REPLIES.get(t.category.key, []),
            today=wf.today_ist(now))

    @app.route("/tickets/<int:ticket_id>/<action>", methods=["POST"])
    @login_required()
    def ticket_action(ticket_id, action):
        if action not in ACTIONS:
            abort(404)
        t = db.get_or_404(Ticket, ticket_id)
        if not wf.can_view(g.user, t):
            abort(403)
        try:
            ACTIONS[action](g.user, t, request.form, utcnow(), request.form.get("version"))
            flash("Saved.", "success")
        except wf.WorkflowError as e:
            db.session.rollback()
            flash(str(e), "danger")
        if not wf.can_view(g.user, t):  # e.g. transferred out of your department
            flash(f"{t.code} is now with {t.department.name}.", "info")
            return redirect(url_for("home"))
        return redirect(url_for("ticket", ticket_id=t.id))

    # ---- Admin ----------------------------------------------------------------------
    @app.route("/dashboard")
    @login_required(ADMIN)
    def dashboard():
        now = utcnow()
        return render_template("dashboard.html", d=reports.dashboard(now), now=now)

    @app.route("/admin/users")
    @login_required(ADMIN)
    def users():
        role = request.args.get("role", "")
        q = (request.args.get("q") or "").strip().lower()
        query = User.query.order_by(User.role, User.name)
        if role in ROLE_LABELS:
            query = query.filter_by(role=role)
        people = [u for u in query.all() if not q or q in u.name.lower() or q in u.username
                  or q in (u.roll_no or "").lower()]
        return render_template("users.html", people=people, role=role, q=q,
                               loads={u.id: wf.workload(u) for u in people if u.role == STAFF})

    @app.route("/admin/users/new", methods=["GET", "POST"])
    @login_required(ADMIN)
    def new_user():
        if request.method == "POST":
            try:
                user = accounts.create_user(g.user, request.form)
                flash(f"{ROLE_LABELS[user.role]} {user.name} added.", "success")
                return redirect(url_for("users"))
            except wf.WorkflowError as e:
                db.session.rollback()
                flash(str(e), "danger")
        return render_template("user_form.html", user=None, form=request.form,
                               role=request.form.get("role") or request.args.get("role", STUDENT),
                               departments=Department.query.order_by(Department.name).all(),
                               permissions=PERMISSIONS)

    @app.route("/admin/users/<int:user_id>", methods=["GET", "POST"])
    @login_required(ADMIN)
    def edit_user(user_id):
        user = db.get_or_404(User, user_id)
        if request.method == "POST":
            try:
                moved = accounts.update_user(g.user, user, request.form, utcnow())
                msg = f"{user.name} updated."
                if moved:
                    msg += f" {moved} open ticket(s) handed over."
                flash(msg, "success")
                return redirect(url_for("users"))
            except wf.WorkflowError as e:
                db.session.rollback()
                flash(str(e), "danger")
        return render_template("user_form.html", user=user, form=request.form, role=user.role,
                               departments=Department.query.order_by(Department.name).all(),
                               permissions=PERMISSIONS)

    @app.route("/admin/users/<int:user_id>/password", methods=["POST"])
    @login_required(ADMIN)
    def reset_password(user_id):
        user = db.get_or_404(User, user_id)
        try:
            accounts.reset_password(g.user, user, request.form.get("password"), request.form.get("confirm"))
            if user.id == g.user.id:
                session["ver"] = user.session_version
            flash(f"Password reset for {user.name}.", "success")
        except wf.WorkflowError as e:
            db.session.rollback()
            flash(str(e), "danger")
        return redirect(url_for("edit_user", user_id=user.id))


if __name__ == "__main__":
    create_app().run(debug=True)
