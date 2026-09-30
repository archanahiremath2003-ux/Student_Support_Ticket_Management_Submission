
from collections import defaultdict
from datetime import timedelta

from models import Ticket, User, STAFF, NEW, WAITING_STUDENT, WAITING_OTHER, RESOLVED, TERMINAL, ACTIVE_WORK
from workflow import calendar, sla_info, age_days, age_bucket, AGE_BUCKETS, hold_overdue


def pct(part, whole):
    return round(100 * part / whole) if whole else None


def dashboard(now):
    cal = calendar()
    tickets = Ticket.query.all()
    info = {t.id: sla_info(t, now, cal) for t in tickets}
    open_work = [t for t in tickets if t.status not in TERMINAL and t.status != RESOLVED]
    month_ago = now - timedelta(days=30)
    solved = [t for t in tickets if t.resolved_at and t.resolved_at >= month_ago]
    replies = [info[t.id]["response"] for t in tickets if t.created_at >= month_ago]
    replied_in_time = replies.count("met")
    replies_due = replied_in_time + replies.count("late") + replies.count("overdue")

    stats = {
        "open": len(open_work),
        "unassigned": sum(t.status == NEW for t in open_work),
        "breached": sum(info[t.id]["state"] == "breached" for t in open_work),
        "at_risk": sum(info[t.id]["state"] == "at_risk" for t in open_work),
        "waiting_student": sum(t.status == WAITING_STUDENT for t in open_work),
        "on_hold": sum(t.status == WAITING_OTHER for t in open_work),
        "hold_overdue": sum(bool(hold_overdue(t, now)) for t in open_work),
        "to_confirm": sum(t.status == RESOLVED for t in tickets),
        "solved_30": len(solved),
        "in_time_pct": pct(sum(info[t.id]["state"] == "met" for t in solved), len(solved)),
        "reply_pct": pct(replied_in_time, replies_due),
        "avg_hours": round(sum(info[t.id]["used"] for t in solved) / len(solved) / 60, 1) if solved else None,
        "avg_days": round(sum((t.resolved_at - t.created_at).total_seconds() for t in solved)
                          / len(solved) / 86400, 1) if solved else None,
        "reopen_pct": pct(sum(t.reopen_count > 0 for t in solved), len(solved)),
    }

    ageing = defaultdict(lambda: {b: 0 for b in AGE_BUCKETS})
    for t in open_work:
        ageing[t.department.name][age_bucket(age_days(t, now))] += 1

    staff_rows = []
    for s in User.query.filter_by(role=STAFF).order_by(User.department_id, User.name).all():
        mine = [t for t in tickets if t.assignee_id == s.id]
        active = [t for t in mine if t.status in ACTIVE_WORK]
        staff_rows.append({"user": s, "open": len(active),
                           "breached": sum(info[t.id]["state"] == "breached" for t in active),
                           "solved_30": sum(1 for t in mine if t.resolved_at and t.resolved_at >= month_ago)})

    escalated = sorted([t for t in tickets if t.escalation_level and t.status not in TERMINAL],
                       key=lambda t: (t.escalation_acked_level >= t.escalation_level,
                                      -t.escalation_level, t.created_at))
    return {"stats": stats, "info": info, "ageing": sorted(ageing.items()), "buckets": AGE_BUCKETS,
            "staff_rows": staff_rows, "escalated": escalated,
            "oldest": sorted(open_work, key=lambda t: t.created_at)[:5],
            "bounced": [t for t in open_work if t.transfer_count >= 2]}
