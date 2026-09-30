"""Business-hours calendar for SLA calculations.

All datetimes stored in the database are naive UTC. The college works in
IST (UTC+05:30, no daylight saving), Monday to Saturday, 09:30 to 17:30,
excluding holidays. Every SLA number in the system is expressed in
business minutes on this calendar.
"""
from datetime import datetime, date, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
WORK_START = time(9, 30)
WORK_END = time(17, 30)
WORKDAYS = {0, 1, 2, 3, 4, 5}  # Monday=0 ... Saturday=5, Sunday off
MINUTES_PER_WORKDAY = 480


def utcnow():
    """Current time as naive UTC (the storage format)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_ist(dt_utc_naive):
    return dt_utc_naive.replace(tzinfo=timezone.utc).astimezone(IST)


def from_ist(dt_ist_aware):
    return dt_ist_aware.astimezone(timezone.utc).replace(tzinfo=None)


class BusinessCalendar:
    def __init__(self, holidays=()):
        self.holidays = set(holidays)

    def is_working_day(self, d: date) -> bool:
        return d.weekday() in WORKDAYS and d not in self.holidays

    @staticmethod
    def _window(d: date):
        start = datetime.combine(d, WORK_START, tzinfo=IST)
        end = datetime.combine(d, WORK_END, tzinfo=IST)
        return start, end

    def minutes_between(self, start_utc, end_utc) -> float:
        """Business minutes elapsed between two naive-UTC datetimes."""
        if end_utc <= start_utc:
            return 0.0
        start, end = to_ist(start_utc), to_ist(end_utc)
        total_seconds = 0.0
        d = start.date()
        while d <= end.date():
            if self.is_working_day(d):
                ws, we = self._window(d)
                lo, hi = max(ws, start), min(we, end)
                if hi > lo:
                    total_seconds += (hi - lo).total_seconds()
            d += timedelta(days=1)
        return total_seconds / 60.0

    def add_minutes(self, start_utc, minutes: float):
        """Return the naive-UTC moment that is `minutes` business minutes
        after `start_utc`. Starting outside hours rolls to the next opening."""
        if minutes <= 0:
            return start_utc
        cursor = to_ist(start_utc)
        remaining = float(minutes)
        for _ in range(3660):  # hard stop: ~10 years of days
            d = cursor.date()
            if self.is_working_day(d):
                ws, we = self._window(d)
                if cursor < ws:
                    cursor = ws
                if cursor < we:
                    available = (we - cursor).total_seconds() / 60.0
                    if remaining <= available:
                        return from_ist(cursor + timedelta(minutes=remaining))
                    remaining -= available
            next_day = d + timedelta(days=1)
            cursor = datetime.combine(next_day, time(0, 0), tzinfo=IST)
        raise ValueError("Could not place deadline; check the holiday calendar")


def humanize_minutes(minutes: float) -> str:
    """Business minutes as a short human string, e.g. '1d 3h' (1d = 8 working hours)."""
    minutes = int(round(abs(minutes)))
    days, rem = divmod(minutes, MINUTES_PER_WORKDAY)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins and not days:
        parts.append(f"{mins}m")
    return " ".join(parts) or "0m"


def friendly(minutes: int) -> str:
    """SLA targets in words: 120 -> '2 working hours', 960 -> '2 working days'."""
    if minutes % MINUTES_PER_WORKDAY == 0:
        n = minutes // MINUTES_PER_WORKDAY
        return f"{n} working day" + ("s" if n != 1 else "")
    hours = minutes / 60
    n = int(hours) if hours == int(hours) else round(hours, 1)
    return f"{n} working hour" + ("s" if n != 1 else "")
