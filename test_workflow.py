"""Ticket rules and the office-hours calendar."""
import random
from datetime import date, timedelta

import pytest

import workflow as wf
from models import db, SlaPolicy, NEW, ASSIGNED, IN_PROGRESS, WAITING_STUDENT, WAITING_OTHER, RESOLVED, CLOSED
from sla import BusinessCalendar
from conftest import ist

MON = (2026, 9, 21)
CAL = BusinessCalendar([date(2026, 10, 2)])


def fee_ticket(w, at=None):
    return wf.create_ticket(w.arjun, w.fees.id, "Refund", "Refund please", "Paid twice", at or ist(*MON, 10))


def sla(t, at):
    return wf.sla_info(t, at, wf.calendar())


# ---- Office-hours calendar --------------------------------------------------------
def test_calendar_counts_only_office_hours():
    assert CAL.minutes_between(ist(*MON, 10), ist(*MON, 12)) == 120
    assert CAL.minutes_between(ist(*MON, 17), ist(2026, 9, 22, 10)) == 60      # overnight
    assert CAL.minutes_between(ist(2026, 9, 26, 17), ist(2026, 9, 28, 10)) == 60  # Sat works, Sun off
    assert CAL.minutes_between(ist(2026, 10, 1, 17), ist(2026, 10, 3, 10)) == 60  # holiday skipped
    assert CAL.minutes_between(ist(*MON, 18), ist(*MON, 23)) == 0


def test_calendar_deadlines():
    assert CAL.add_minutes(ist(2026, 9, 27, 20), 60) == ist(2026, 9, 28, 10, 30)  # Sunday night
    assert CAL.add_minutes(ist(*MON, 9, 30), 480) == ist(*MON, 17, 30)
    rng = random.Random(1)
    for _ in range(200):
        start = ist(*MON, 9, 30) + timedelta(minutes=rng.randint(0, 60 * 24 * 20))
        mins = rng.randint(1, 5000)
        assert abs(CAL.minutes_between(start, CAL.add_minutes(start, mins)) - mins) < 1e-6


# ---- Creating and assigning ----------------------------------------------------------
def test_ticket_is_routed_and_given_to_least_busy_staff(w):
    a, b, c = fee_ticket(w), fee_ticket(w), fee_ticket(w)
    assert a.department == w.accounts and a.priority == "high" and a.status == ASSIGNED
    assert [a.assignee, b.assignee, c.assignee] == [w.ramesh, w.lakshmi, w.ramesh]


def test_bad_ticket_input_is_rejected(w):
    with pytest.raises(wf.WorkflowError):
        wf.create_ticket(w.arjun, w.fees.id, "Refund", "  ", "details", ist(*MON))
    with pytest.raises(wf.WorkflowError):
        wf.create_ticket(w.arjun, w.fees.id, "Made up type", "t", "d", ist(*MON))
    with pytest.raises(wf.PermissionDenied):
        wf.create_ticket(w.ramesh, w.fees.id, "Refund", "t", "d", ist(*MON))


def test_editing_policy_does_not_move_existing_deadlines(w):
    t = fee_ticket(w)
    db.session.get(SlaPolicy, "high").resolution_minutes = 60
    db.session.commit()
    assert t.resolution_minutes == 960


# ---- Status rules ---------------------------------------------------------------------
def test_finished_ticket_cannot_restart(w):
    t = fee_ticket(w)
    wf.resolve(w.ramesh, t, "Completed", "Done", ist(*MON, 11))
    wf.confirm(w.arjun, t, ist(*MON, 12))
    assert t.status == CLOSED
    with pytest.raises(wf.WorkflowError):
        wf.start_work(w.ramesh, t, ist(*MON, 13))
    with pytest.raises(wf.WorkflowError):
        wf.reopen(w.arjun, t, "again", ist(*MON, 13))


def test_resolve_needs_a_note_and_reopen_goes_back_to_same_person(w):
    t = fee_ticket(w)
    with pytest.raises(wf.WorkflowError):
        wf.resolve(w.ramesh, t, "Completed", "", ist(*MON, 11))
    wf.resolve(w.ramesh, t, "Completed", "Done", ist(*MON, 11))
    wf.reopen(w.arjun, t, "Still showing due", ist(*MON, 12))
    assert t.status == IN_PROGRESS and t.assignee == w.ramesh and t.reopen_count == 1


# ---- Time limits -----------------------------------------------------------------------
def test_clock_pauses_while_waiting_for_student(w):
    t = fee_ticket(w)
    wf.request_info(w.ramesh, t, "Send the UTR number", ist(*MON, 11))
    wf.comment(w.arjun, t, "UTR 1234", False, ist(2026, 9, 23, 11))  # two days later
    assert t.status == IN_PROGRESS
    assert sla(t, ist(2026, 9, 23, 11))["used"] == 60


def test_transfer_keeps_clock_and_assigns_in_new_department(w):
    t = wf.create_ticket(w.arjun, w.idcard.id, "New card", "Hostel fee link broken", "error", ist(*MON, 10))
    assert t.assignee == w.girish
    w.girish.can_transfer = True
    wf.transfer(w.girish, t, w.accounts.id, "Fee matter", ist(*MON, 12))
    assert t.department_id == w.accounts.id and t.assignee in (w.ramesh, w.lakshmi)
    assert sla(t, ist(*MON, 13))["used"] == 180


def test_priority_change_counts_from_creation(w):
    t = fee_ticket(w)
    wf.change_priority(w.admin, t, "urgent", "Exam registration closes today", ist(*MON, 11))
    assert t.resolution_minutes == 480
    assert sla(t, ist(*MON, 17, 30))["state"] == "at_risk"          # 450 of 480
    assert sla(t, ist(2026, 9, 22, 10, 1))["state"] == "breached"


def test_internal_note_is_not_a_first_reply(w):
    t = fee_ticket(w)
    wf.comment(w.ramesh, t, "checking bank statement", True, ist(*MON, 10, 30))
    assert sla(t, ist(*MON, 12, 30))["response"] == "overdue"
    wf.comment(w.ramesh, t, "Looking into it", False, ist(*MON, 13))
    assert sla(t, ist(*MON, 13))["response"] == "late"


# ---- Escalation and automatic steps -----------------------------------------------------
def test_escalates_once_then_critical(w):
    t = fee_ticket(w)
    wf.comment(w.ramesh, t, "On it", False, ist(*MON, 10, 30))
    wf.run_automations(ist(2026, 9, 23, 10, 30))
    wf.run_automations(ist(2026, 9, 23, 10, 45))
    esc = [e for e in t.events if e.kind == "escalated"]
    assert t.escalation_level == 1 and len(esc) == 1
    assert esc[0].created_at == ist(2026, 9, 23, 10, 0)  # when the limit was actually crossed
    wf.run_automations(ist(2026, 9, 24, 10))
    assert t.escalation_level == 2
    assert wf.needs_action_by(w.admin, t, ist(2026, 9, 24, 10))
    wf.acknowledge(w.admin, t, "Lakshmi to fix today", ist(2026, 9, 24, 11))
    assert not wf.needs_action_by(w.admin, t, ist(2026, 9, 24, 11))


def test_unassigned_ticket_escalates_when_nobody_is_free(w):
    w.ramesh.available = w.lakshmi.available = False
    db.session.commit()
    t = fee_ticket(w)
    assert t.status == NEW
    wf.run_automations(ist(*MON, 12, 30))
    assert t.escalation_level == 1


def test_reminder_then_auto_resolve_then_auto_close(w):
    t = fee_ticket(w)
    wf.request_info(w.ramesh, t, "Send the UTR", ist(*MON, 10, 30))
    wf.run_automations(ist(2026, 9, 23, 11))
    wf.run_automations(ist(2026, 9, 23, 12))
    assert sum(e.kind == "reminder" for e in t.events) == 1
    wf.run_automations(ist(2026, 9, 26, 11))
    assert t.status == RESOLVED
    wf.run_automations(ist(2026, 10, 3, 11))  # 3 office days later (Friday 2 Oct is a holiday)
    assert t.status == CLOSED


def test_hold_needs_follow_up_date_and_returns_when_it_passes(w):
    t = fee_ticket(w)
    with pytest.raises(wf.WorkflowError):
        wf.put_on_hold(w.ramesh, t, "Bank", None, ist(*MON, 11))
    with pytest.raises(wf.WorkflowError):
        wf.put_on_hold(w.ramesh, t, "Bank", "2026-12-31", ist(*MON, 11))
    wf.put_on_hold(w.ramesh, t, "Waiting on bank", "2026-09-23", ist(*MON, 11))
    assert t.status == WAITING_OTHER
    assert not wf.needs_action_by(w.ramesh, t, ist(2026, 9, 23, 12))
    assert wf.needs_action_by(w.ramesh, t, ist(2026, 9, 24, 10))


# ---- Staff rights given by the admin -------------------------------------------------------
def test_staff_need_rights_for_extra_actions(w):
    t = fee_ticket(w)  # Ramesh owns it
    with pytest.raises(wf.PermissionDenied):
        wf.reject(w.ramesh, t, "Duplicate", ist(*MON, 11))
    with pytest.raises(wf.PermissionDenied):
        wf.change_priority(w.ramesh, t, "urgent", "why", ist(*MON, 11))
    w.ramesh.can_reject = True
    wf.reject(w.ramesh, t, "Duplicate", ist(*MON, 11))
    assert t.status == "rejected"


def test_only_owner_or_admin_can_work_a_ticket(w):
    t = fee_ticket(w)
    with pytest.raises(wf.PermissionDenied):
        wf.resolve(w.lakshmi, t, "Completed", "not mine", ist(*MON, 11))
    with pytest.raises(wf.PermissionDenied):
        wf.comment(w.arjun, t, "secret", True, ist(*MON, 11))
    with pytest.raises(wf.PermissionDenied):
        wf.confirm(w.priya, t, ist(*MON, 11))
    wf.resolve(w.admin, t, "Completed", "Done by admin", ist(*MON, 11))


def test_assign_right_and_same_department_rule(w):
    t = fee_ticket(w)
    with pytest.raises(wf.PermissionDenied):
        wf.assign(w.ramesh, t, w.lakshmi.id, "swap", ist(*MON, 11))
    w.ramesh.can_assign = True
    with pytest.raises(wf.WorkflowError):
        wf.assign(w.ramesh, t, w.girish.id, "other dept", ist(*MON, 11))
    wf.assign(w.ramesh, t, w.lakshmi.id, "Ramesh is at the bank", ist(*MON, 11))
    assert t.assignee == w.lakshmi


def test_leave_hands_tickets_over_or_back_to_queue(w):
    t1, t2 = fee_ticket(w), fee_ticket(w)
    assert wf.release_tickets(w.ramesh, w.admin, ist(*MON, 12), "leave") == 1
    w.ramesh.available = False
    assert t1.assignee == w.lakshmi
    wf.release_tickets(w.lakshmi, w.admin, ist(*MON, 12, 5), "leave")
    assert t1.status == NEW and t1.assignee is None and t2.status == NEW


def test_stale_form_is_rejected(w):
    t = fee_ticket(w)
    old = t.version
    wf.start_work(w.ramesh, t, ist(*MON, 11), old)
    with pytest.raises(wf.ConflictError):
        wf.resolve(w.ramesh, t, "Completed", "Done", ist(*MON, 12), old)
    assert t.status == IN_PROGRESS
