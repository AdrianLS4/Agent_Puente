"""Pruebas del receptor de webhooks FastAPI y scheduler de timeouts."""
from datetime import timedelta
import pytest
from starlette.testclient import TestClient

from puente.cli import build
from puente.db import iso, utcnow
from puente.server import create_app
from puente.state_machine import State as S


@pytest.fixture
def test_setup():
    conn, orch, gw, clock = build(mock=True)
    # create_app sin scheduler de fondo en tests para control determinista
    app = create_app(conn=conn, orch=orch, enable_scheduler=False)
    client = TestClient(app)
    return conn, orch, gw, clock, client


def test_health_check(test_setup):
    _, _, _, _, client = test_setup
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "requests_by_state" in data


def test_webhook_checkout_order_approved_triggers_capture(test_setup):
    conn, orch, gw, clock, client = test_setup

    # 1. Crear solicitud hasta AWAITING_PROVIDER_APPROVAL
    rid = orch.create_request(1, 1, "0102", "key-wh-1").data["request_id"]
    orch.quote(rid)
    orch.select_quote(rid, 1)
    q = orch._row("SELECT total_ves FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)", rid)
    orch.confirm_ves(rid, "REF-WH-1", q["total_ves"])
    o = orch.create_paypal_order(rid)
    order_id = o.data["paypal_order_id"]

    # Simular que el comprador aprobó en PayPal
    gw.approve(order_id)

    # 2. Simular webhook CHECKOUT.ORDER.APPROVED enviado por PayPal
    webhook_payload = {
        "id": "WH-EVT-001",
        "event_version": "1.0",
        "create_time": iso(utcnow()),
        "resource_type": "checkout-order",
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource": {
            "id": order_id,
            "status": "APPROVED",
        },
    }

    resp = client.post("/webhooks/paypal", json=webhook_payload)
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["status"] == "success"
    assert res_data["action"] == "order_captured"

    # 3. Verificar que el orquestador capturó y avanzó a AWAITING_DELIVERY
    req = conn.execute("SELECT state FROM purchase_requests WHERE id=?", (rid,)).fetchone()
    assert req["state"] == S.AWAITING_DELIVERY


def test_webhook_idempotency_ignores_duplicates(test_setup):
    conn, orch, gw, clock, client = test_setup

    rid = orch.create_request(1, 1, "0102", "key-wh-dup").data["request_id"]
    orch.quote(rid)
    orch.select_quote(rid, 1)
    q = orch._row("SELECT total_ves FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)", rid)
    orch.confirm_ves(rid, "REF-WH-DUP", q["total_ves"])
    o = orch.create_paypal_order(rid)
    order_id = o.data["paypal_order_id"]
    gw.approve(order_id)

    payload = {
        "id": "WH-SAME-EVENT-ID",
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource": {"id": order_id},
    }

    # Primer envío: procesado
    r1 = client.post("/webhooks/paypal", json=payload)
    assert r1.status_code == 200
    assert r1.json()["status"] == "success"

    # Segundo envío (reintento idéntico): ignorado con 200 OK
    r2 = client.post("/webhooks/paypal", json=payload)
    assert r2.status_code == 200
    assert r2.json()["status"] == "ignored"
    assert r2.json()["reason"] == "duplicate_event"


def test_scheduler_tick_endpoint(test_setup):
    conn, orch, gw, clock, client = test_setup

    rid = orch.create_request(1, 1, "0102", "key-tick").data["request_id"]
    orch.quote(rid)

    # Avanzar el reloj para que venza la cotización
    clock.advance(minutes=20)

    resp = client.post("/scheduler/tick")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert any(a[0] == rid and a[2] == "expired" for a in data["acted"])

    # Verificar que pasó a EXPIRED
    req = conn.execute("SELECT state FROM purchase_requests WHERE id=?", (rid,)).fetchone()
    assert req["state"] == S.EXPIRED


def test_webhook_invoice_paid_triggers_completion(test_setup):
    conn, orch, gw, clock, client = test_setup
    rid = orch.create_request(1, 2, "0102", "key-wh-inv").data["request_id"]
    orch.quote(rid)
    orch.select_quote(rid, 1)
    q = orch._row("SELECT total_ves FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)", rid)
    orch.confirm_ves(rid, "REF-WH-INV", q["total_ves"])
    inv_res = orch.create_paypal_invoice(rid)
    iid = inv_res.data["paypal_invoice_id"]
    gw.pay_invoice(iid)

    webhook_payload = {
        "id": "WH-INV-PAID-001",
        "event_type": "INVOICING.INVOICE.PAID",
        "resource": {"id": iid},
    }
    resp = client.post("/webhooks/paypal", json=webhook_payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert resp.json()["action"] == "invoice_paid"

    req = conn.execute("SELECT state FROM purchase_requests WHERE id=?", (rid,)).fetchone()
    assert req["state"] == S.AWAITING_DELIVERY
