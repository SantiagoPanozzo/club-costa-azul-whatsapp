"""Deterministic presentation only; never use these labels as API/state values."""

import re
from datetime import date, datetime, time

# .NET DayOfWeek is Sunday-first, unlike Python date.weekday().
_DAYS = ("Dom", "Lun", "Mar", "Mié", "Jue", "Vie", "Sáb")
_NAMES = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
_FULL_DAYS = ("domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado")
_DAY_NUMBERS = {name: i for i, name in enumerate(_NAMES)}
_DAY_NUMBERS.update({name[:3]: i for i, name in enumerate(_NAMES)})
_DAY_NUMBERS.update({str(i): i for i in range(7)})


def format_weekday(value: object) -> str:
    """Accept API enums and legacy numeric enums; omit unrecognized values."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        token = value.strip().lower()
        number = _DAY_NUMBERS.get(token, -1)
    else:
        return ""
    return _DAYS[number] if 0 <= number < 7 else ""


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def format_date(value: object, fallback: str = "Sin fecha") -> str:
    """Render the supplied calendar date without changing its timezone."""
    parsed = _date(value)
    return f"{parsed.day:02d}/{parsed.month:02d}/{parsed.year:04d}" if parsed else fallback


def format_date_weekday(value: object) -> str:
    parsed = _date(value)
    if parsed is None:
        return "Sin fecha"
    return f"{_FULL_DAYS[(parsed.weekday() + 1) % 7]} {format_date(parsed)}"


def format_time(value: object, fallback: str = "Sin definir") -> str:
    try:
        parsed = value if isinstance(value, time) else time.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    return f"{parsed.hour:02d}:{parsed.minute:02d}" if parsed else fallback


def format_time_range(start: object, end: object, fallback: str = "Horario no informado") -> str:
    first, last = format_time(start, ""), format_time(end, "")
    if not first and not last:
        return fallback
    # A missing endpoint must not look like a complete, known schedule.
    return f"{first or '?'} - {last or '?'}"


def format_schedule(activity: dict) -> str:
    schedules = activity.get("horarios")
    if isinstance(schedules, list) and schedules:
        groups: dict[str, list[str]] = {}
        for schedule in schedules:
            if not isinstance(schedule, dict):
                continue
            day = format_weekday(schedule.get("dia"))
            hours = format_time_range(schedule.get("horaInicio"), schedule.get("horaFin"), "")
            if day or hours:
                days = groups.setdefault(hours, [])
                if day and day not in days:
                    days.append(day)
        if groups:
            return "; ".join(" ".join(p for p in (", ".join(days), hours) if p) for hours, days in groups.items())
    values = activity.get("diasSemana")
    days = ", ".join(filter(None, map(format_weekday, values))) if isinstance(values, list) else ""
    hours = format_time_range(activity.get("horaInicio"), activity.get("horaFin"), "")
    return " ".join(p for p in (days, hours) if p) or "Horario no definido"


def format_billing_period(cuota: dict) -> str:
    start, end = _date(cuota.get("fechaInicio")), _date(cuota.get("fechaFin"))
    if start and end:
        return f"{format_date(start)} - {format_date(end)}"
    # Month-only legacy periods have no known day; don't invent one.
    period = str(cuota.get("periodo") or "")
    if re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])(?:_\d{4}-(?:0[1-9]|1[0-2]))?", period):
        return " - ".join(f"{part[5:7]}/{part[:4]}" for part in period.split("_"))
    return period or "Período no informado"
