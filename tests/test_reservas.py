from app.flows.reservas import _reservables


def test_espacio_disponible_sin_campo_reservable_se_ofrece():
    espacios = [{"id": "e1", "nombre": "Salón", "estado": "Disponible"}]

    assert _reservables(espacios) == espacios


def test_espacio_disponible_reservable_se_ofrece():
    espacios = [{"id": "e1", "nombre": "Salón", "estado": "Disponible", "reservable": True}]

    assert _reservables(espacios) == espacios


def test_espacio_disponible_no_reservable_no_se_ofrece():
    espacios = [{"id": "e1", "nombre": "Salón de talleres", "estado": "Disponible", "reservable": False}]

    assert _reservables(espacios) == []


def test_espacio_en_mantenimiento_no_se_ofrece():
    espacios = [{"id": "e1", "nombre": "Salón", "estado": "Mantenimiento", "reservable": True}]

    assert _reservables(espacios) == []


def test_mezcla_de_espacios_conserva_solo_los_reservables():
    reservable = {"id": "e1", "nombre": "Salón", "estado": "Disponible"}
    tambien_reservable = {"id": "e2", "nombre": "Quincho", "estado": "Disponible", "reservable": True}
    no_reservable = {"id": "e3", "nombre": "Depósito", "estado": "Disponible", "reservable": False}
    en_mantenimiento = {"id": "e4", "nombre": "Cancha", "estado": "Mantenimiento", "reservable": True}

    assert _reservables([reservable, tambien_reservable, no_reservable, en_mantenimiento]) == [
        reservable,
        tambien_reservable,
    ]
