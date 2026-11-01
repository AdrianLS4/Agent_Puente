"""Pruebas de punta a punta con MockPayPal (sin credenciales ni red)."""
from datetime import timedelta

import pytest

from puente import policy
from puente.cli import build, pay_and_capture
from puente.errors import CATALOG, Action, map_paypal_error, resolve
from puente.paypal_gateway import PayPalError
from puente.state_machine import InvalidTransition, State as S, TRANSITIONS


def quote_and_select(orch, key="k", item=1):
    rid = orch.create_request(1, item, "0102", key).data["request_id"]
    assert orch.quote(rid).ok
    assert orch.select_quote(rid, 1).ok
    return rid


def total(orch, rid):
    return orch._row("SELECT total_ves FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)", rid)["total_ves"]


# ---------- flujo feliz ----------
def test_happy_path_completes_and_updates_provider():
    conn, orch, gw, _ = build()
    rid = quote_and_select(orch)
    pid = orch._row("SELECT provider_id FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)", rid)["provider_id"]
    before = conn.execute("SELECT completed_orders, usd_capacity_available FROM providers WHERE id=?", (pid,)).fetchone()
    assert orch.confirm_ves(rid, "R1", total(orch, rid)).ok
    o = orch.create_paypal_order(rid)
    gw.approve(o.data["paypal_order_id"])
    assert orch.capture(rid).state == S.AWAITING_DELIVERY
    assert orch.record_delivery(rid, proof="KEY", delivered=True).state == S.DELIVERED
    assert orch.confirm_receipt(rid).state == S.COMPLETED
    after = conn.execute("SELECT completed_orders, usd_capacity_available FROM providers WHERE id=?", (pid,)).fetchone()
    assert after["completed_orders"] == before["completed_orders"] + 1
    assert after["usd_capacity_available"] == before["usd_capacity_available"] - 29.0   # capacidad reservada
    assert conn.execute("SELECT COUNT(*) c FROM events WHERE request_id=?", (rid,)).fetchone()["c"] >= 8


def test_invoice_flow_completes():
    conn, orch, gw, _ = build()
    rid = quote_and_select(orch, key="k-inv", item=2)
    assert orch.confirm_ves(rid, "R-INV", total(orch, rid)).ok
    inv_res = orch.create_paypal_invoice(rid)
    assert inv_res.ok
    assert inv_res.state == S.AWAITING_PROVIDER_APPROVAL
    iid = inv_res.data["paypal_invoice_id"]

    # Simular que la factura aún no fue pagada
    assert not orch.check_invoice_paid(rid).ok

    # Simular pago de la factura
    gw.pay_invoice(iid)
    paid_res = orch.check_invoice_paid(rid)
    assert paid_res.ok
    assert paid_res.state == S.AWAITING_DELIVERY

    assert orch.record_delivery(rid, proof="HASH-INV", delivered=True).state == S.DELIVERED
    assert orch.confirm_receipt(rid).state == S.COMPLETED
    assert conn.execute("SELECT kind, status FROM paypal_orders WHERE request_id=?", (rid,)).fetchone()["kind"] == "invoice"


# ---------- idempotencia ----------
def test_idempotent_request_and_capture():
    _, orch, gw, _ = build()
    a = orch.create_request(1, 1, "0102", "same")
    b = orch.create_request(1, 1, "0102", "same")
    assert a.data["request_id"] == b.data["request_id"] and b.code == "E_IDEMPOTENT_REPLAY"
    rid = quote_and_select(orch, "k2")
    orch.confirm_ves(rid, "R2", total(orch, rid))
    o = orch.create_paypal_order(rid)
    assert orch.create_paypal_order(rid).data["paypal_order_id"] == o.data["paypal_order_id"]   # no duplica orden
    gw.approve(o.data["paypal_order_id"])
    assert orch.capture(rid).ok
    captures = [c for c in gw.calls if c[0] == "capture_order"]
    assert len(captures) == 1


# ---------- errores del catálogo ----------
def test_retries_with_backoff_then_success():
    _, orch, gw, _ = build()
    sleeps = []
    orch.sleep = sleeps.append
    rid = quote_and_select(orch)
    orch.confirm_ves(rid, "R3", total(orch, rid))
    gw.fail_next["create_order"] = [PayPalError(429), PayPalError(429), PayPalError(503)]
    assert orch.create_paypal_order(rid).ok
    assert sleeps == [2, 4, 8]          # backoff exponencial: 2·2^intento (el contador de intentos es compartido)


def test_retries_exhausted_goes_to_human():
    conn, orch, gw, _ = build()
    rid = quote_and_select(orch)
    orch.confirm_ves(rid, "R4", total(orch, rid))
    gw.fail_next["create_order"] = [PayPalError(429)] * 10
    r = orch.create_paypal_order(rid)
    assert not r.ok and r.state == S.HUMAN_REVIEW
    assert conn.execute("SELECT COUNT(*) c FROM human_review_queue").fetchone()["c"] == 1


def test_instrument_declined_waits_then_succeeds():
    _, orch, gw, _ = build()
    rid = quote_and_select(orch)
    orch.confirm_ves(rid, "R5", total(orch, rid))
    oid = orch.create_paypal_order(rid).data["paypal_order_id"]
    gw.approve(oid)
    gw.fail_next["capture_order"] = [PayPalError(422, "UNPROCESSABLE_ENTITY", "INSTRUMENT_DECLINED")]
    r = orch.capture(rid)
    assert not r.ok and r.code == "E_PP_INSTRUMENT_DECLINED" and r.state == S.AWAITING_PROVIDER_APPROVAL
    assert orch.capture(rid).state == S.AWAITING_DELIVERY


def test_already_captured_race_is_success():
    _, orch, gw, _ = build()
    rid = quote_and_select(orch)
    orch.confirm_ves(rid, "R6", total(orch, rid))
    oid = orch.create_paypal_order(rid).data["paypal_order_id"]
    gw.approve(oid)
    gw.fail_next["capture_order"] = [PayPalError(422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED")]
    gw.orders[oid].update(status="COMPLETED", capture_id="MOCK-CAP-X")
    assert orch.capture(rid).state == S.AWAITING_DELIVERY


def test_capture_pending_does_not_deliver():
    _, orch, gw, _ = build()
    rid = quote_and_select(orch)
    orch.confirm_ves(rid, "R7", total(orch, rid))
    oid = orch.create_paypal_order(rid).data["paypal_order_id"]
    gw.approve(oid)
    gw.set_capture_status(oid, "PENDING")
    r = orch.capture(rid)
    assert not r.ok and r.code == "E_PP_CAPTURE_PENDING" and r.state == S.AWAITING_PROVIDER_APPROVAL
    gw.set_capture_status(oid, "COMPLETED")             # llega el webhook de COMPLETED
    assert orch.capture(rid).state == S.AWAITING_DELIVERY


def test_amount_mismatch_escalates():
    _, orch, gw, _ = build()
    rid = quote_and_select(orch)
    orch.confirm_ves(rid, "R8", total(orch, rid))
    oid = orch.create_paypal_order(rid).data["paypal_order_id"]
    gw.approve(oid)
    gw.orders[oid]["amount"] = 1.0
    r = orch.capture(rid)
    assert r.state == S.HUMAN_REVIEW and r.code == "E_PP_AMOUNT_MISMATCH"


def test_ves_duplicate_reference_and_mismatch():
    conn, orch, gw, _ = build()
    r1 = quote_and_select(orch, "a")
    assert orch.confirm_ves(r1, "DUP", total(orch, r1)).ok
    r2 = quote_and_select(orch, "b")
    assert orch.confirm_ves(r2, "DUP", total(orch, r2)).code == "E_VES_DUPLICATE_REF"
    r3 = quote_and_select(orch, "c")
    assert orch.confirm_ves(r3, "OK3", total(orch, r3) * 0.9).code == "E_VES_AMOUNT_MISMATCH"
    assert orch._req(r3)["state"] == S.HUMAN_REVIEW


def test_ves_timeout_expires_without_money_movement():
    conn, orch, gw, clock = build()
    rid = quote_and_select(orch)
    clock.advance(minutes=31)
    orch.check_timeouts()
    assert orch._req(rid)["state"] == S.EXPIRED and gw.calls == []


def test_expired_quote_with_big_fx_drift_refunds_ves():
    conn, orch, gw, clock = build()
    rid = quote_and_select(orch)
    clock.advance(minutes=14)                              # > ttl(10)+gracia(5)? no: 14 < 15
    orch.fx_fetch = lambda: (45.0, "mock")                 # +12% de deriva
    clock.advance(minutes=2)
    r = orch.confirm_ves(rid, "LATE", total(orch, rid))
    assert r.state == S.REFUNDED and r.code == "E_QUOTE_EXPIRED"


def test_failover_refunds_penalizes_and_reopens():
    conn, orch, gw, clock = build()
    rid = quote_and_select(orch)
    pid = orch._row("SELECT provider_id FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)", rid)["provider_id"]
    cap_before = conn.execute("SELECT usd_capacity_available c FROM providers WHERE id=?", (pid,)).fetchone()["c"]
    orch.confirm_ves(rid, "FO", total(orch, rid))
    orch.create_paypal_order(rid)
    clock.advance(minutes=16)
    assert orch.check_timeouts()[0][2] == "E_PP_NOT_APPROVED"
    assert orch._req(rid)["state"] == S.REFUNDED
    assert conn.execute("SELECT status FROM ves_payments WHERE request_id=?", (rid,)).fetchone()["status"] == "refunded"
    assert conn.execute("SELECT failed_orders FROM providers WHERE id=?", (pid,)).fetchone()["failed_orders"] >= 1
    assert conn.execute("SELECT usd_capacity_available c FROM providers WHERE id=?", (pid,)).fetchone()["c"] == cap_before
    child = conn.execute("SELECT * FROM purchase_requests WHERE parent_request_id=?", (rid,)).fetchone()
    assert child is not None
    assert orch.quote(child["id"]).ok
    assert pid not in [x["provider_id"] for x in conn.execute("SELECT provider_id FROM quotes WHERE request_id=?", (child["id"],))]


def test_failover_chain_is_capped():
    conn, orch, gw, clock = build()
    rid = quote_and_select(orch)
    for i in range(3):
        orch.confirm_ves(rid, f"CH{i}", total(orch, rid))
        orch.create_paypal_order(rid)
        clock.advance(minutes=16)
        orch.check_timeouts()
        child = conn.execute("SELECT id FROM purchase_requests WHERE parent_request_id=?", (rid,)).fetchone()
        if child is None:
            break
        rid = child["id"]
        assert orch.quote(rid).ok
        orch.select_quote(rid, 1)
    assert conn.execute("SELECT COUNT(*) c FROM purchase_requests").fetchone()["c"] <= 1 + 2   # original + 2 failovers


def test_delivery_timeout_refunds_paypal_and_ves():
    conn, orch, gw, clock = build()
    rid = quote_and_select(orch)
    orch.confirm_ves(rid, "DT", total(orch, rid))
    oid = orch.create_paypal_order(rid).data["paypal_order_id"]
    gw.approve(oid)
    orch.capture(rid)
    clock.advance(hours=25)
    orch.check_timeouts()
    assert orch._req(rid)["state"] == S.REFUNDED
    assert "refund_capture" in [c[0] for c in gw.calls]


def test_amount_limit_blocks_before_quoting():
    _, orch, _, _ = build()
    orch.conn.execute("UPDATE catalog_items SET price_usd=500 WHERE id=1")
    r = orch.create_request(1, 1, "0102", "big")
    assert not r.ok and r.code == "E_AMOUNT_LIMIT"


def test_no_providers_fails_cleanly():
    conn, orch, gw, _ = build()
    conn.execute("UPDATE providers SET status='paused'")
    rid = orch.create_request(1, 1, "0102", "np").data["request_id"]
    r = orch.quote(rid)
    assert r.code == "E_NO_PROVIDERS" and r.state == S.FAILED


# ---------- disputas ----------
class FakeBrain:
    def __init__(self, decision): self.decision = decision
    def decide_dispute(self, ctx, allowed): return {"decision": self.decision, "rationale_es": "test"}


def _dispute_setup(brain=None, item=1, delivered=True, hours=72):
    conn, orch, gw, clock = build(brain=brain)
    rid = quote_and_select(orch, f"d{item}{delivered}", item)
    pay_and_capture_quiet(orch, gw, rid)
    if delivered:
        orch.record_delivery(rid, proof="KEY", delivered=True)
    return conn, orch, gw, clock, rid


def pay_and_capture_quiet(orch, gw, rid):
    orch.confirm_ves(rid, f"DR{rid}", total(orch, rid))
    gw.approve(orch.create_paypal_order(rid).data["paypal_order_id"])
    assert orch.capture(rid).ok


def test_dispute_strong_proof_contests():
    conn, orch, gw, clock, rid = _dispute_setup()
    r = orch.open_dispute(rid, "D1", "NOT_RECEIVED", clock() + timedelta(hours=72))
    assert r.state == S.DISPUTE_CONTESTED and "provide_evidence" in [c[0] for c in gw.calls]
    assert orch.resolve_dispute_outcome(rid, "won").state == S.COMPLETED


def test_dispute_lost_refunds_ves():
    conn, orch, gw, clock, rid = _dispute_setup()
    orch.open_dispute(rid, "D2", "NOT_RECEIVED", clock() + timedelta(hours=72))
    assert orch.resolve_dispute_outcome(rid, "lost").state == S.REFUNDED


def test_dispute_no_proof_low_amount_accepts():
    conn, orch, gw, clock, rid = _dispute_setup(delivered=False)
    r = orch.open_dispute(rid, "D3", "NOT_RECEIVED", clock() + timedelta(hours=72))
    assert r.state == S.REFUNDED and "accept_claim" in [c[0] for c in gw.calls]


def test_dispute_high_value_goes_to_human():
    conn, orch, gw, clock, rid = _dispute_setup(item=2, delivered=False)
    r = orch.open_dispute(rid, "D4", "NOT_RECEIVED", clock() + timedelta(hours=72))
    assert r.state == S.HUMAN_REVIEW and r.code == "E_DISPUTE_HIGH_VALUE"


def test_dispute_deadline_near_goes_to_human():
    conn, orch, gw, clock, rid = _dispute_setup()
    r = orch.open_dispute(rid, "D5", "NOT_RECEIVED", clock() + timedelta(hours=10))
    assert r.state == S.HUMAN_REVIEW and r.code == "E_DISPUTE_DEADLINE_NEAR"


def test_llm_guardrail_blocks_forbidden_action():
    # prueba fuerte -> permitido {contest, human}; el LLM intenta 'accept' => bloqueado
    conn, orch, gw, clock, rid = _dispute_setup(brain=FakeBrain("accept"))
    r = orch.open_dispute(rid, "D6", "NOT_RECEIVED", clock() + timedelta(hours=72))
    assert r.state == S.HUMAN_REVIEW
    assert "accept_claim" not in [c[0] for c in gw.calls]
    assert conn.execute("SELECT COUNT(*) c FROM error_events WHERE code='E_GUARDRAIL_BLOCKED'").fetchone()["c"] == 1


def test_llm_allowed_choice_is_used():
    conn, orch, gw, clock, rid = _dispute_setup(brain=FakeBrain("contest"))
    orch.open_dispute(rid, "D7", "NOT_RECEIVED", clock() + timedelta(hours=72))
    assert conn.execute("SELECT decided_by FROM disputes").fetchone()["decided_by"] == "agent"


# ---------- máquina de estados / catálogo ----------
def test_invalid_transition_rejected():
    _, orch, _, _ = build()
    rid = orch.create_request(1, 1, "0102", "x").data["request_id"]
    with pytest.raises(InvalidTransition):
        orch._to(rid, S.PAID)


def test_every_state_is_defined_and_reachable():
    states = set(TRANSITIONS)
    targets = {t for v in TRANSITIONS.values() for t in v}
    assert targets <= states
    assert states - targets == {S.CREATED}


def test_catalog_consistency():
    for code, spec in CATALOG.items():
        assert spec.user_msg_es and spec.playbook, code
        if spec.next_state:
            assert spec.next_state in TRANSITIONS, code
        if spec.retryable:
            assert spec.max_retries > 0
    assert resolve("E_PP_RATE_LIMIT", 0).action == Action.RETRY
    assert resolve("E_PP_RATE_LIMIT", 4).action == Action.HUMAN_REVIEW
    assert resolve("NO_EXISTE").code == "E_UNKNOWN"


@pytest.mark.parametrize("status,issue,code", [
    (401, "", "E_PP_AUTH"), (429, "", "E_PP_RATE_LIMIT"), (503, "", "E_PP_NETWORK"), (None, "", "E_PP_NETWORK"),
    (422, "ORDER_ALREADY_CAPTURED", "E_PP_ALREADY_CAPTURED"), (422, "INSTRUMENT_DECLINED", "E_PP_INSTRUMENT_DECLINED"),
    (422, "ORDER_NOT_APPROVED", "E_PP_NOT_APPROVED"), (422, "OTHER", "E_PP_UNPROCESSABLE"), (404, "", "E_PP_NOT_FOUND"),
])
def test_paypal_error_mapping(status, issue, code):
    assert map_paypal_error(status, "", issue) == code


def test_policy_matrix():
    from puente.config import Settings
    s = Settings()
    ok = lambda **kw: policy.allowed_dispute_actions(settings=s, **{"amount_usd": 20, "strength": "strong", "hours_left": 100, **kw})
    assert ok()[0] == {"contest", "human"}
    assert ok(strength="weak")[0] == {"contest", "accept", "human"}
    assert ok(strength="none")[0] == {"accept", "human"}
    assert ok(amount_usd=500) == ({"human"}, "E_DISPUTE_HIGH_VALUE")
    assert ok(hours_left=5) == ({"human"}, "E_DISPUTE_DEADLINE_NEAR")
