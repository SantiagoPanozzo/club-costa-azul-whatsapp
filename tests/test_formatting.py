from copy import deepcopy
from datetime import date, datetime, time

import pytest

from app.formatting import (
    format_billing_period,
    format_date,
    format_date_weekday,
    format_schedule,
    format_time,
    format_time_range,
    format_weekday,
)


@pytest.mark.parametrize(
    "number,name,label",
    [
        (0, "Sunday", "Dom"),
        (1, "Monday", "Lun"),
        (2, "Tuesday", "Mar"),
        (3, "Wednesday", "Mié"),
        (4, "Thursday", "Jue"),
        (5, "Friday", "Vie"),
        (6, "Saturday", "Sáb"),
    ],
)
def test_weekday_enum_variants(number, name, label):
    for value in (number, str(number), name, name.lower(), name[:3], f" {name[:3].upper()} "):
        assert format_weekday(value) == label


@pytest.mark.parametrize("value", [None, "Funday", "Mondayish", "", -1, 7, "99", True, False, 1.0, [], {}])
def test_unknown_weekday_is_omitted(value):
    assert format_weekday(value) == ""


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2024-02-29", "29/02/2024"),
        ("2026-11-09T00:30:00Z", "09/11/2026"),
        ("2026-11-09T23:30:00-03:00", "09/11/2026"),
        (date(2026, 11, 9), "09/11/2026"),
        (datetime(2026, 11, 9, 12), "09/11/2026"),
        ("0001-01-01", "01/01/0001"),
    ],
)
def test_dates_do_not_convert_timezone(value, expected):
    assert format_date(value) == expected


@pytest.mark.parametrize("value", [None, "", "2026-02-29", "2026-13-01", "Monday", [], {}, 20261109])
def test_invalid_date_uses_context_fallback(value):
    assert format_date(value) == "Sin fecha"
    assert format_date(value, "—") == "—"


def test_python_weekdays_are_not_dotnet_enums():
    assert format_date_weekday("2026-11-09") == "lunes 09/11/2026"
    assert format_date_weekday("2026-11-08") == "domingo 08/11/2026"
    assert format_date_weekday("2026-11-11") == "miércoles 11/11/2026"
    assert format_date_weekday(None) == "Sin fecha"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("09:05:00", "09:05"),
        ("09:05:59.1234567", "09:05"),
        ("23:59", "23:59"),
        ("00:00:00", "00:00"),
        (time(8, 3), "08:03"),
        ("09:05:00+03:00", "09:05"),
    ],
)
def test_time_precision_is_presentation_only(value, expected):
    assert format_time(value) == expected


@pytest.mark.parametrize("value", [None, "", "25:00", "09:60", "nine", [], {}, 900])
def test_invalid_times_use_spanish_fallback(value):
    assert format_time(value) == "Sin definir"


def test_partial_and_absent_ranges_are_explicit():
    assert format_time_range("09:00:00", "10:00:00") == "09:00 - 10:00"
    assert format_time_range("09:00:00", None) == "09:00 - ?"
    assert format_time_range(None, "10:00:00") == "? - 10:00"
    assert format_time_range(None, None) == "Horario no informado"


@pytest.mark.parametrize("days", [[1, 3], ["Monday", "Wednesday"], ["mon", "3"], [1, "Funday", 3]])
def test_legacy_activity_schedule(days):
    assert format_schedule({"diasSemana": days, "horaInicio": "09:00:00", "horaFin": "10:00:00"}) == (
        "Lun, Mié 09:00 - 10:00"
    )


def test_api_per_day_hours_override_legacy_hours_without_mutation():
    activity = {
        "diasSemana": ["Monday", "Wednesday", "Friday"],
        "horaInicio": "09:00:00",
        "horaFin": "10:00:00",
        "horarios": [
            {"dia": "Monday", "horaInicio": "09:00:00", "horaFin": "10:00:00"},
            {"dia": "Wednesday", "horaInicio": "18:00:00", "horaFin": "19:00:00"},
            {"dia": "Friday", "horaInicio": "09:00:00", "horaFin": "10:00:00"},
        ],
    }
    original = deepcopy(activity)
    assert format_schedule(activity) == "Lun, Vie 09:00 - 10:00; Mié 18:00 - 19:00"
    assert activity == original


@pytest.mark.parametrize("schedules", [None, [], [None, {}], "invalid"])
def test_absent_or_unusable_per_day_schedule_falls_back(schedules):
    assert format_schedule({"horarios": schedules, "diasSemana": [0]}) == "Dom"
    assert format_schedule({}) == "Horario no definido"
    assert format_schedule({"diasSemana": ["Funday"]}) == "Horario no definido"


@pytest.mark.parametrize(
    "cuota,expected",
    [
        (
            {"periodo": "2026-01_2026-06", "fechaInicio": "2026-01-15", "fechaFin": "2026-06-30"},
            "15/01/2026 - 30/06/2026",
        ),
        ({"periodo": "2026-01_2026-06"}, "01/2026 - 06/2026"),
        ({"periodo": "2026-11"}, "11/2026"),
        ({"periodo": "2026-11", "fechaInicio": "invalid", "fechaFin": "2026-11-30"}, "11/2026"),
        ({"periodo": "Período especial"}, "Período especial"),
        ({}, "Período no informado"),
    ],
)
def test_billing_period_keeps_known_precision_and_custom_labels(cuota, expected):
    original = deepcopy(cuota)
    assert format_billing_period(cuota) == expected
    assert cuota == original
