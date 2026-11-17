"""Opening hours and delivery-zone checks."""

from datetime import datetime

import pytest

from app import main


def at(monkeypatch, hh: int, mm: int = 0) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 10, hh, mm, tzinfo=tz)

    monkeypatch.setattr(main, "datetime", FrozenDatetime)


@pytest.mark.parametrize(
    ("hh", "expected"),
    [(9, False), (10, True), (15, True), (22, True), (23, False)],
)
def test_daytime_hours(monkeypatch, hh, expected):
    at(monkeypatch, hh, 30)
    ok, *_ = main._is_open({"opens": "10:00", "closes": "23:00"})
    assert ok is expected


@pytest.mark.parametrize(("hh", "expected"), [(23, True), (1, True), (12, False)])
def test_hours_across_midnight(monkeypatch, hh, expected):
    at(monkeypatch, hh)
    ok, *_ = main._is_open({"opens": "18:00", "closes": "02:00"})
    assert ok is expected


def test_ignore_hours_accepts_orders_at_night(monkeypatch):
    at(monkeypatch, 3)
    ok, *_ = main._is_open({"opens": "10:00", "closes": "23:00", "ignore_hours": True})
    assert ok is True


def check(api, address: str) -> dict:
    return api.post("/api/delivery/check", json={"address": address}).json()


def test_address_in_zone_is_accepted(api):
    assert check(api, "Каспийск, ул. Орджоникидзе 5")["можно доставить"] is True


def test_town_outside_zone_is_rejected(api):
    r = check(api, "Буйнакск")
    assert r["можно доставить"] is False
    assert r["причина"] == "Возим по Махачкале и Каспийску."


def test_street_without_city_is_accepted(api):
    # Callers often name only the street; the city is implied.
    assert check(api, "улица Гагарина, дом 10")["можно доставить"] is True


def test_closed_kitchen_rejects_delivery(api, settings, monkeypatch):
    settings.update(ignore_hours=False, opens="10:00", closes="23:00")
    at(monkeypatch, 3)
    r = check(api, "Махачкала, ул. Гагарина 10")
    assert r["можно доставить"] is False
    assert "закрыто" in r["причина"]
