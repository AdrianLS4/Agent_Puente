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


def test_p2p_api_intent_and_escrow_flow(test_setup):
    """Prueba de la API P2P con IA: lenguaje natural -> ofertas + explicación IA -> Escrow -> Pago Móvil -> Liberar."""
    conn, orch, gw, clock, client = test_setup

    # 1. María envía su intención en lenguaje natural
    intent_resp = client.post("/p2p/intent", json={
        "text": "Hola, quiero comprar el curso de Python que cuesta 29 dólares y tengo cuenta en Banco de Venezuela",
        "customer_id": 1,
    })
    assert intent_resp.status_code == 200
    intent_data = intent_resp.json()
    rid = intent_data["request_id"]
    assert intent_data["state"] == S.QUOTED
    assert intent_data["parsed_intent"]["amount_usd"] == 29.0
    assert len(intent_data["quotes"]) >= 1
    assert "Recomiendo" in intent_data["ai_explanation"]   # IA presente y activa

    # 2. María elige al proveedor #1 -> se crea orden Escrow (retención)
    sel_resp = client.post(f"/p2p/requests/{rid}/select", json={"rank": 1})
    assert sel_resp.status_code == 200
    oid = sel_resp.json()["paypal_order_id"]

    # 3. Carlos aprueba en PayPal y se bloquean los fondos en PayPal Escrow
    gw.approve(oid)
    lock_resp = client.post(f"/p2p/requests/{rid}/lock")
    assert lock_resp.status_code == 200
    assert lock_resp.json()["state"] == S.USD_ESCROW_LOCKED

    # 4. María consulta el ticket de Pago Móvil con datos seguros de Carlos
    ticket_resp = client.get(f"/p2p/requests/{rid}/ticket")
    assert ticket_resp.status_code == 200
    ticket = ticket_resp.json()["ticket"]
    assert ticket["usd_locked"] is True
    assert ticket["bank_code"] == "0102"
    assert "0414" in ticket["phone"] or "0412" in ticket["phone"]

    # 5. María paga en su banco e ingresa la referencia
    pay_resp = client.post(f"/p2p/requests/{rid}/confirm-ves", json={
        "reference": "REF-BDV-123456",
        "amount_ves": ticket["total_ves"],
    })
    assert pay_resp.status_code == 200

    # 6. Carlos confirma que recibió los Bolívares y PUENTE libera los USD al comercio
    release_resp = client.post(f"/p2p/requests/{rid}/release")
    assert release_resp.status_code == 200
    assert release_resp.json()["state"] == "COMPLETED"

    # Verificar estado final en la base de datos
    db_req = conn.execute("SELECT state FROM purchase_requests WHERE id=?", (rid,)).fetchone()
    assert db_req["state"] == S.COMPLETED


class _ScriptedWebLLM:
    def __init__(self, turns):
        self.turns = list(turns)

    def complete(self, messages, tools):
        return self.turns.pop(0)


def test_puente_ui_three_column_full_flow():
    """Prueba de punta a punta de todos los endpoints consumidos por puente-ui (api.ts)."""
    conn, orch, gw, clock = build(mock=True)
    llm = _ScriptedWebLLM([
        {
            "tool_calls": [
                {"function": {"name": "get_quotes", "arguments": {"item_id": 1, "bank": "0102"}}},
                {"function": {"name": "propose_purchase", "arguments": {"rank": 1}}},
            ]
        },
        {"content": "Encontré proveedores verificados y te propongo el #1."},
        {"content": "Orden creada. Le avisé al proveedor para que bloquee el pago en PayPal."},
    ])
    app = create_app(conn=conn, orch=orch, llm=llm, enable_scheduler=False)
    client = TestClient(app)

    # 1. Cliente chatea en la columna izquierda
    r_chat = client.post("/chat", json={"session_id": "sid-ui-1", "text": "Quiero el curso de Python con BDV"})
    assert r_chat.status_code == 200
    chat_data = r_chat.json()
    assert len(chat_data["trace"]) == 2
    assert chat_data["trace"][0]["name"] == "get_quotes"
    assert chat_data["trace"][1]["name"] == "propose_purchase"
    assert len(chat_data["offers"]) >= 1
    assert chat_data["pending"]["rank"] == 1
    assert chat_data["requestId"] is None

    # 2. Cliente confirma la propuesta en su tarjeta
    r_conf = client.post("/chat/sid-ui-1/confirm")
    assert r_conf.status_code == 200
    conf_data = r_conf.json()
    rid = conf_data["requestId"]
    assert rid is not None
    assert conf_data["trace"][0]["name"] == "confirm_purchase"

    # 3. UI consulta la vista de la solicitud (Estado + bandejas)
    r_view1 = client.get(f"/p2p/requests/{rid}/view")
    assert r_view1.status_code == 200
    v1 = r_view1.json()
    assert v1["state"] == "AWAITING_PROVIDER_APPROVAL"
    assert v1["payment"] is None  # Aún no bloqueado en PayPal
    assert any(m["template"] == "lock_request" and m["role"] == "provider" for m in v1["inbox"])

    # 4. Proveedor aprueba en PayPal desde su columna
    r_app = client.post(f"/demo/provider/approve/{rid}")
    assert r_app.status_code == 200
    v2 = client.get(f"/p2p/requests/{rid}/view").json()
    assert v2["state"] == "AWAITING_VES"
    assert v2["payment"] is not None
    assert v2["payment"]["totalVes"] == v2["totalVes"]

    # 5. Cliente reporta referencia equivocada -> proveedor rechaza por wrong_reference -> vuelve a AWAITING_VES
    r_rep1 = client.post(f"/p2p/requests/{rid}/report", json={
        "bank_reference": "00009999",
        "amount_ves": v2["totalVes"],
        "payer_bank": "Banesco",
        "payer_phone": "04141234567",
    })
    assert r_rep1.status_code == 200
    v3 = client.get(f"/p2p/requests/{rid}/view").json()
    assert v3["state"] == "VES_REPORTED"
    assert v3["report"]["reference"] == "00009999"

    r_rej = client.post(f"/p2p/requests/{rid}/provider/reject", json={"reason": "wrong_reference"})
    assert r_rej.status_code == 200
    v4 = client.get(f"/p2p/requests/{rid}/view").json()
    assert v4["state"] == "AWAITING_VES"
    assert v4["report"] is None
    assert any(m["template"] == "ref_mismatch" for m in v4["inbox"])

    # 6. Cliente reporta referencia correcta y proveedor confirma
    r_rep2 = client.post(f"/p2p/requests/{rid}/report", json={
        "bank_reference": "98765432",
        "amount_ves": v2["totalVes"],
        "payer_bank": "Banesco",
        "payer_phone": "04141234567",
    })
    assert r_rep2.status_code == 200

    r_pconf = client.post(f"/p2p/requests/{rid}/provider/confirm")
    assert r_pconf.status_code == 200
    v5 = client.get(f"/p2p/requests/{rid}/view").json()
    assert v5["state"] == "COMPLETED"
    assert any(m["template"] == "paid" for m in v5["inbox"])
    assert any(m["template"] == "captured" for m in v5["inbox"])

    # 7. Reiniciar demo
    r_reset = client.post("/demo/reset")
    assert r_reset.status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM purchase_requests").fetchone()[0] == 0

