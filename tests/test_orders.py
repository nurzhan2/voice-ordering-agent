"""Order lifecycle through the HTTP API: the guards the agent relies on."""

from fastapi.testclient import TestClient

from app import main

CALL = "call-1"


def add(api, item_id: str, **body):
    return api.post(f"/api/calls/{CALL}/items", json={"item_id": item_id, **body})


def fill_customer(api, **overrides):
    body = {
        "name": "Марат",
        "address": "Махачкала, ул. Гагарина 10",
        "persons": 2,
        "payment": "transfer",
    }
    body.update(overrides)
    return api.patch(f"/api/calls/{CALL}/customer", json=body)


def test_requests_without_token_are_rejected(conn):
    anon = TestClient(main.app)
    assert anon.get(f"/api/calls/{CALL}/draft").status_code == 401


def test_invented_dish_cannot_be_added(api, store):
    # The model only gets an ID from a database lookup; anything else is rejected.
    r = add(api, "soy-sauce")
    assert r.status_code == 422
    assert CALL not in store


def test_sized_dish_requires_a_size(api, store):
    r = add(api, "pizza").json()
    assert r["error"] == "нужен размер"
    assert [s["size"] for s in r["доступные размеры"]] == ["25 см", "33 см"]
    assert CALL not in store


def test_size_sets_the_price(api):
    r = add(api, "pizza", size="33 см").json()
    assert r["итого"] == 750


def test_same_dish_twice_merges_into_one_line(api):
    add(api, "kebab")
    r = add(api, "kebab").json()
    assert len(r["позиции"]) == 1
    assert r["позиции"][0]["количество"] == 2
    assert r["итого"] == 1780


def test_draft_lists_what_is_still_missing(api):
    r = add(api, "kebab").json()
    assert set(r["не хватает"]) == {
        "имя гостя",
        "адрес доставки",
        "на сколько персон приборы",
        "способ оплаты",
    }
    r = fill_customer(api).json()
    assert r["не хватает"] == []


def test_upsell_stops_after_two_offers(api):
    for kind in ("garnish", "sauce"):
        api.post(f"/api/calls/{CALL}/offered", json={"kind": kind})
    # FakeConnection.fetch raises, so this also proves no menu query was made.
    r = api.get("/api/menu/recommend", params={"kind": "drink", "call_id": CALL}).json()
    assert r["предлагать самой"] is False
    assert r["блюда в наличии"] is True


def test_confirm_rereads_prices_from_the_database(api, menu, conn, store):
    add(api, "kebab", quantity=2)
    fill_customer(api)
    # The price changes mid-call; the draft in Redis still holds the old one.
    menu["kebab"]["price_cents"] = 95000

    r = api.post(f"/api/calls/{CALL}/confirm").json()

    assert r["итого"] == 1900
    assert len(conn.orders) == 1
    assert CALL not in store, "draft must be dropped once the order exists"


def test_confirm_fails_when_a_dish_ran_out(api, menu, conn):
    add(api, "kebab")
    fill_customer(api)
    menu["kebab"]["is_available"] = False

    r = api.post(f"/api/calls/{CALL}/confirm").json()

    assert "закончилась" in r["error"]
    assert conn.orders == []


def test_confirm_requires_an_address(api, conn):
    add(api, "kebab")
    fill_customer(api, address=None)

    r = api.post(f"/api/calls/{CALL}/confirm").json()

    assert r["error"] == "нет адреса доставки"
    assert conn.orders == []


def test_confirm_enforces_minimum_order(api, settings, conn):
    settings["min_order"] = 1000
    add(api, "fries")
    fill_customer(api)

    r = api.post(f"/api/calls/{CALL}/confirm").json()

    assert r["error"] == "сумма меньше минимальной"
    assert r["не хватает"] == 775
    assert conn.orders == []


def test_empty_order_cannot_be_confirmed(api):
    assert api.post(f"/api/calls/{CALL}/confirm").status_code == 422
