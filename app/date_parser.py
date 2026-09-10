import re
from datetime import date

_FULL_DATE = re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})")
_DAY_MONTH = re.compile(r"(\d{1,2})[/\-.](\d{1,2})")
_DAY_ONLY = re.compile(r"\b(\d{1,2})\b")


def parse_flexible_date(text: str) -> date | None:
    text = text.strip()

    m = _FULL_DATE.search(text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None

    m = _DAY_MONTH.search(text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        try:
            return date(date.today().year, month, day)
        except ValueError:
            return None

    m = _DAY_ONLY.search(text)
    if m:
        day = int(m.group(1))
        today = date.today()
        try:
            return date(today.year, today.month, day)
        except ValueError:
            return None

    return None
