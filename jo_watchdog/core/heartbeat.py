"""Pure core: which 24-hour window the daily heartbeat covers (#25)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo


def heartbeat_window(now: datetime, heartbeat_time: str, zone: tzinfo) -> tuple[datetime, datetime]:
    """``[start, end)``: the 24 hours ending at the latest ``heartbeat_time`` not after ``now``.

    Boundaries are wall-clock times in ``zone``, so the window stays 07:00 to
    07:00 local even across a UTC date change.
    """
    hour, minute = (int(part) for part in heartbeat_time.split(":"))
    local = now.astimezone(zone)

    def boundary(day: date) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=zone)

    end = boundary(local.date())
    if end > local:
        end = boundary(local.date() - timedelta(days=1))
    start = boundary(end.date() - timedelta(days=1))
    return start, end
