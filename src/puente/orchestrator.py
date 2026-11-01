"""Orquestador determinista. Mueve el dinero y las transiciones; el LLM (brain) solo opina donde se le pide.

    cotizar → elegir → confirmar Bs → crear orden PayPal → capturar → entregar/tracking → completar
                                  ↘ errores del catálogo ↙        ↘ disputas (política + LLM) ↙
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

from . import policy
from .config import Settings
from .db import iso, log_event, parse_iso, utcnow
from .errors import Action, Resolution, map_paypal_error, resolve
from .fx import FxError, Fetcher, get_reference_rate
from .paypal_gateway import PayPalError, PayPalGateway
from .ranking import ProviderProfile, rank_providers
from .state_machine import InvalidTransition, State as S, get_state, transition


class Brain(Protocol):
    def decide_dispute(self, ctx: dict, allowed: set[str]) -> dict: ...   # {"decision","rationale_es"}


@dataclass
class StepResult:
    ok: bool
    state: str
    message_es: str = ""
    code: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class StepFailure(Exception):
    def __init__(self, resolution: Resolution):
        super().__init__(resolution.code)
        self.resolution = resolution


class Orchestrator:
    def __init__(
        self,
        conn: sqlite3.Connection,
        gateway: PayPalGateway,
        settings: Settings,
        fx_fetch: Fetcher,
        *,
        brain: Brain | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
        ves_refunder: Callable[[int, float], bool] | None = None,
    ):
        self.conn, self.gw, self.s, self.fx_fetch = conn, gateway, settings, fx_fetch
        self.brain, self.sleep, self.clock = brain, sleep, clock
        self.ves_refunder = ves_refunder or (lambda request_id, amount: True)   # demo: siempre OK

    # ───────────────────────── utilidades internas ─────────────────────────
    def _row(self, sql: str, *args) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def _req(self, rid: int) -> sqlite3.Row:
        return self._row("SELECT * FROM purchase_requests WHERE id=?", rid)

    def _to(self, rid: int, state: str, **kw) -> None:
        transition(self.conn, rid, state, self.s, now=self.clock(), **kw)

    def _log_error(self, rid: int | None, res: Resolution, attempt: int, detail: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO error_events (request_id, code, action, attempt, detail, created_at) VALUES (?,?,?,?,?,?)",
            (rid, res.code, res.action.value, attempt, json.dumps(detail, default=str) if detail else None,
             iso(self.clock())))
        self.conn.commit()

    def _queue_human(self, rid: int, code: str, note: str = "") -> None:
        self.conn.execute("INSERT INTO human_review_queue (request_id, reason_code, note) VALUES (?,?,?)",
                          (rid, code, note))
        self.conn.commit()

    def _escalate(self, rid: int, res: Resolution, note: str = "") -> StepResult:
        self._queue_human(rid, res.code, note or res.spec.playbook)
        try:
            self._to(rid, S.HUMAN_REVIEW, actor="system", event_type="escalated", payload={"code": res.code})
        except InvalidTransition:
            pass
        return StepResult(False, get_state(self.conn, rid), res.user_msg_es, res.code)

    def _paypal_call(self, rid: int, fn: Callable[[], dict]) -> dict:
        """Llama a PayPal con la política del catálogo: reintentos con backoff o StepFailure."""
        attempt = 0
        while True:
            try:
                return fn()
            except PayPalError as e:
                code = map_paypal_error(e.status_code, e.name, e.issue)
                res = resolve(code, attempt)
                self._log_error(rid, res, attempt, {"status": e.status_code, "name": e.name,
                                                    "issue": e.issue, "debug_id": e.debug_id})
                if res.action == Action.RETRY:
                    attempt += 1
                    self.sleep(res.delay_s)
                    continue
                raise StepFailure(res) from e

    def _apply_failure(self, rid: int, res: Resolution) -> StepResult:
        """Ejecuta la acción que el catálogo prescribe para `res`."""
        a = res.action
        if a == Action.WAIT:
            return StepResult(False, get_state(self.conn, rid), res.user_msg_es, res.code)
        if a == Action.ABORT:
            if res.next_state:
                self._to(rid, res.next_state, event_type="aborted", payload={"code": res.code})
            return StepResult(False, get_state(self.conn, rid), res.user_msg_es, res.code)
        if a == Action.FAILOVER_PROVIDER:
            return self._failover(rid, res)
        if a == Action.COMPENSATE:
            return self.compensate(rid, refund_paypal=True, res=res)
        if a == Action.TREAT_AS_SUCCESS:
            return StepResult(True, get_state(self.conn, rid), res.user_msg_es, res.code)
        return self._escalate(rid, res)          # HUMAN_REVIEW y cualquier caso no previsto

    # ───────────────────────── 1. Solicitud y cotización ─────────────────────────
    def create_request(self, customer_id: int, item_id: int, bank: str | None, idempotency_key: str,
                       *, parent_request_id: int | None = None, excluded: list[int] | None = None) -> StepResult:
        dup = self._row("SELECT id, state FROM purchase_requests WHERE idempotency_key=?", idempotency_key)
        if dup:
            res = resolve("E_IDEMPOTENT_REPLAY")
            return StepResult(True, dup["state"], res.user_msg_es, res.code, {"request_id": dup["id"]})
        item = self._row("SELECT * FROM catalog_items WHERE id=? AND active=1", item_id)
        if item is None:
            raise ValueError(f"item {item_id} no existe")
        if item["price_usd"] > self.s.max_amount_usd:
            res = resolve("E_AMOUNT_LIMIT")
            self._log_error(None, res, 0, {"amount": item["price_usd"]})
            return StepResult(False, S.CREATED, res.user_msg_es, res.code)
        cur = self.conn.execute(
            "INSERT INTO purchase_requests (idempotency_key, parent_request_id, excluded_providers, customer_id,"
            " merchant_id, item_id, amount_usd, customer_bank, state, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (idempotency_key, parent_request_id, json.dumps(excluded or []), customer_id, item["merchant_id"],
             item_id, item["price_usd"], bank, S.CREATED, iso(self.clock()), iso(self.clock())))
        rid = cur.lastrowid
        log_event(self.conn, rid, "request_created", actor="customer", now=self.clock())
        self.conn.commit()
        return StepResult(True, S.CREATED, "Solicitud creada.", None, {"request_id": rid})

    def _load_providers(self) -> list[ProviderProfile]:
        out = []
        for r in self.conn.execute("SELECT * FROM providers"):
            out.append(ProviderProfile(
                id=r["id"], display_name=r["display_name"], kyc_level=r["kyc_level"], status=r["status"],
                paypal_verified=bool(r["paypal_verified"]), completed_orders=r["completed_orders"],
                disputed_orders=r["disputed_orders"], failed_orders=r["failed_orders"],
                avg_settle_minutes=r["avg_settle_minutes"], usd_capacity_available=r["usd_capacity_available"],
                margin_pct=r["margin_pct"], fee_usd=r["fee_usd"], accepts_banks=set(json.loads(r["accepts_banks"]))))
        return out

    def quote(self, rid: int) -> StepResult:
        req = self._req(rid)
        self._to(rid, S.QUOTING)
        try:
            fx = get_reference_rate(self.conn, self.fx_fetch, self.clock(), self.s.fx_max_age_minutes)
        except FxError:
            res = resolve("E_FX_UNAVAILABLE")
            self._log_error(rid, res, 0)
            return self._apply_failure(rid, res)
        result = rank_providers(
            self._load_providers(), amount_usd=req["amount_usd"], bank=req["customer_bank"], ref_rate=fx.rate,
            min_kyc=self.s.min_kyc_level, exclude=set(json.loads(req["excluded_providers"])))
        if not result.quotes:
            res = resolve("E_NO_PROVIDERS")
            self._log_error(rid, res, 0, {"rejected": result.rejected})
            return self._apply_failure(rid, res)
        expires = self.clock() + timedelta(minutes=self.s.quote_ttl_minutes)
        for q in result.quotes:
            self.conn.execute(
                "INSERT INTO quotes (request_id, provider_id, ref_rate_ves_per_usd, rate_ves_per_usd, margin_pct,"
                " fee_usd, total_ves, score, score_breakdown, rank, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rid, q.provider_id, q.ref_rate, q.rate_ves_per_usd, q.margin_pct, q.fee_usd, q.total_ves,
                 q.score, json.dumps(q.breakdown), q.rank, iso(expires)))
        self.conn.commit()
        self._to(rid, S.QUOTED, payload={"fx": fx.rate, "fx_fallback": fx.is_fallback,
                                         "rejected": result.rejected})
        return StepResult(True, S.QUOTED, "Cotizaciones listas.", None, {
            "fx_rate": fx.rate, "fx_fallback": fx.is_fallback, "rejected": result.rejected,
            "quotes": [vars(q) for q in result.quotes]})

    def select_quote(self, rid: int, rank: int = 1) -> StepResult:
        q = self._row("SELECT * FROM quotes WHERE request_id=? AND rank=? AND status='offered'", rid, rank)
        if q is None:
            raise ValueError("cotización no disponible")
        now = self.clock()
        if now > parse_iso(q["expires_at"]) + timedelta(minutes=self.s.quote_grace_minutes):
            res = resolve("E_QUOTE_EXPIRED")
            self.conn.execute("UPDATE quotes SET status='expired' WHERE request_id=?", (rid,))
            self._to(rid, S.EXPIRED, event_type="quote_expired")
            return StepResult(False, S.EXPIRED, res.user_msg_es, res.code)
        self.conn.execute("UPDATE quotes SET status='selected' WHERE id=?", (q["id"],))
        self.conn.execute("UPDATE purchase_requests SET selected_quote_id=? WHERE id=?", (q["id"], rid))
        ref = f"PUENTE-{rid}-{secrets.token_hex(3).upper()}"
        self.conn.execute("INSERT INTO ves_payments (request_id, quote_id, status) VALUES (?,?, 'pending')",
                          (rid, q["id"]))
        self.conn.commit()
        self._to(rid, S.AWAITING_VES, payload={"quote_id": q["id"]})
        return StepResult(True, S.AWAITING_VES, "Paga en bolívares para continuar.", None, {
            "provider_id": q["provider_id"], "total_ves": q["total_ves"], "payment_reference_hint": ref,
            "pay_before": iso(now + timedelta(minutes=self.s.ves_confirm_timeout_min))})

    # ───────────────────────── 2. Pago en VES (simulado) ─────────────────────────
    def confirm_ves(self, rid: int, reference: str, amount_ves: float) -> StepResult:
        req = self._req(rid)
        q = self._row("SELECT * FROM quotes WHERE id=?", req["selected_quote_id"])
        pay = self._row("SELECT * FROM ves_payments WHERE request_id=? ORDER BY id DESC LIMIT 1", rid)
        if self._row("SELECT 1 FROM ves_payments WHERE reference=? AND id<>?", reference, pay["id"]):
            res = resolve("E_VES_DUPLICATE_REF")
            self._log_error(rid, res, 0, {"reference": reference})
            return self._apply_failure(rid, res)
        self.conn.execute("UPDATE ves_payments SET reference=?, amount_ves=? WHERE id=?",
                          (reference, amount_ves, pay["id"]))
        if abs(amount_ves - q["total_ves"]) / q["total_ves"] * 100 > self.s.ves_amount_tolerance_pct:
            res = resolve("E_VES_AMOUNT_MISMATCH")
            self._log_error(rid, res, 0, {"expected": q["total_ves"], "received": amount_ves})
            return self._apply_failure(rid, res)
        now = self.clock()
        if now > parse_iso(q["expires_at"]) + timedelta(minutes=self.s.quote_grace_minutes):
            try:
                cur = get_reference_rate(self.conn, self.fx_fetch, now, self.s.fx_max_age_minutes).rate
                drift = abs(cur - q["ref_rate_ves_per_usd"]) / q["ref_rate_ves_per_usd"] * 100
            except FxError:
                drift = float("inf")
            if drift > self.s.fx_drift_tolerance_pct:
                self.conn.execute("UPDATE ves_payments SET status='confirmed', confirmed_at=? WHERE id=?",
                                  (iso(now), pay["id"]))
                res = resolve("E_QUOTE_EXPIRED")
                self._log_error(rid, res, 0, {"drift_pct": drift})
                return self.compensate(rid, refund_paypal=False, res=res)
        self.conn.execute("UPDATE ves_payments SET status='confirmed', confirmed_at=? WHERE id=?",
                          (iso(now), pay["id"]))
        self.conn.commit()
        self._to(rid, S.VES_CONFIRMED, actor="customer", payload={"reference": reference})
        return StepResult(True, S.VES_CONFIRMED, "Pago en Bs. confirmado.")

    # ───────────────────────── 3. Orden PayPal ─────────────────────────
    def create_paypal_order(self, rid: int) -> StepResult:
        req = self._req(rid)
        q = self._row("SELECT * FROM quotes WHERE id=?", req["selected_quote_id"])
        item = self._row("SELECT * FROM catalog_items WHERE id=?", req["item_id"])
        existing = self._row("SELECT * FROM paypal_orders WHERE request_id=? AND status IN ('CREATED','APPROVED')", rid)
        if existing:                                   # idempotencia: no duplicar órdenes
            return StepResult(True, req["state"], "Orden existente.", None,
                              {"paypal_order_id": existing["paypal_order_id"], "approve_url": existing["approve_url"]})
        try:
            out = self._paypal_call(rid, lambda: self.gw.create_order(
                amount_usd=req["amount_usd"], item_name=item["title"], reference_id=f"REQ-{rid}"))
        except StepFailure as sf:
            return self._apply_failure(rid, sf.resolution)
        self.conn.execute(
            "INSERT INTO paypal_orders (request_id, provider_id, kind, paypal_order_id, status, approve_url,"
            " amount_usd, created_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, q["provider_id"], "order", out["order_id"], "CREATED", out.get("approve_url"),
             req["amount_usd"], iso(self.clock()), json.dumps(out.get("raw"), default=str)))
        self.conn.execute("UPDATE providers SET usd_capacity_available = usd_capacity_available - ? WHERE id=?",
                          (req["amount_usd"], q["provider_id"]))          # reserva de capacidad
        self.conn.commit()
        self._to(rid, S.PAYPAL_ORDER_CREATED, payload={"paypal_order_id": out["order_id"]})
        self._to(rid, S.AWAITING_PROVIDER_APPROVAL)
        return StepResult(True, S.AWAITING_PROVIDER_APPROVAL, "El proveedor debe aprobar el pago en PayPal.",
                          None, {"paypal_order_id": out["order_id"], "approve_url": out.get("approve_url")})

    def capture(self, rid: int) -> StepResult:
        """Idempotente: se puede llamar de nuevo tras un webhook/reintento sin doble captura."""
        req = self._req(rid)
        o = self._row("SELECT * FROM paypal_orders WHERE request_id=? ORDER BY id DESC LIMIT 1", rid)
        oid = o["paypal_order_id"]
        try:
            info = self._paypal_call(rid, lambda: self.gw.get_order(oid))
            if info["status"] == "COMPLETED":
                cap = info                                           # ya capturada: éxito idempotente
            elif info["status"] == "APPROVED":
                try:
                    cap = self._paypal_call(rid, lambda: self.gw.capture_order(oid))
                except StepFailure as sf:
                    if sf.resolution.action != Action.TREAT_AS_SUCCESS:
                        raise
                    cap = self._paypal_call(rid, lambda: self.gw.get_order(oid))
            else:
                return StepResult(False, req["state"], "Esperando que el proveedor apruebe en PayPal.",
                                  "WAITING_APPROVAL")
        except StepFailure as sf:
            return self._apply_failure(rid, sf.resolution)

        self.conn.execute("UPDATE paypal_orders SET capture_id=?, captured_amount_usd=? WHERE id=?",
                          (cap.get("capture_id"), cap.get("amount_usd"), o["id"]))
        self.conn.commit()
        if cap.get("capture_status") in ("PENDING",):
            res = resolve("E_PP_CAPTURE_PENDING")
            self._log_error(rid, res, 0)
            return self._apply_failure(rid, res)
        if cap.get("capture_status") in ("DECLINED", "DENIED", "FAILED"):
            return self._apply_failure(rid, resolve("E_PP_NOT_APPROVED"))
        if cap.get("amount_usd") is None or abs(cap["amount_usd"] - req["amount_usd"]) > 0.01:
            res = resolve("E_PP_AMOUNT_MISMATCH")
            self._log_error(rid, res, 0, {"expected": req["amount_usd"], "captured": cap.get("amount_usd")})
            return self._apply_failure(rid, res)

        self.conn.execute("UPDATE paypal_orders SET status='COMPLETED', captured_at=? WHERE id=?",
                          (iso(self.clock()), o["id"]))
        merchant = self._row("SELECT delivery_type FROM merchants WHERE id=?", req["merchant_id"])
        self.conn.execute("INSERT INTO deliveries (request_id, delivery_type, deadline_at) VALUES (?,?,?)",
                          (rid, merchant["delivery_type"],
                           iso(self.clock() + timedelta(hours=self.s.delivery_deadline_hours))))
        self.conn.commit()
        self._to(rid, S.PAID, actor="paypal_webhook", payload={"capture_id": cap.get("capture_id")})
        self._to(rid, S.AWAITING_DELIVERY)
        return StepResult(True, S.AWAITING_DELIVERY, "Pago confirmado. Esperando entrega.", None,
                          {"capture_id": cap.get("capture_id")})

    def create_paypal_invoice(self, rid: int) -> StepResult:
        """Crea y envía una factura de PayPal para la solicitud (vía Invoicing v2)."""
        req = self._req(rid)
        q = self._row("SELECT * FROM quotes WHERE id=?", req["selected_quote_id"])
        provider = self._row("SELECT * FROM providers WHERE id=?", q["provider_id"])
        item = self._row("SELECT * FROM catalog_items WHERE id=?", req["item_id"])

        existing = self._row("SELECT * FROM paypal_orders WHERE request_id=? AND kind='invoice' AND status IN ('DRAFT','SENT')", rid)
        if existing:
            return StepResult(True, req["state"], "Factura existente.", None,
                              {"paypal_invoice_id": existing["paypal_invoice_id"], "invoice_url": existing["invoice_url"]})

        try:
            inv = self._paypal_call(rid, lambda: self.gw.create_invoice(
                amount_usd=req["amount_usd"],
                item_name=item["title"],
                recipient_email=provider["paypal_email"],
                reference_id=f"REQ-{rid}",
                note=f"PUENTE: Compra #{rid} - {item['title']}",
            ))
            iid = inv["invoice_id"]
            sent = self._paypal_call(rid, lambda: self.gw.send_invoice(iid))
        except StepFailure as sf:
            return self._apply_failure(rid, sf.resolution)

        invoice_url = sent.get("invoice_url") or inv.get("invoice_url")
        self.conn.execute(
            "INSERT INTO paypal_orders (request_id, provider_id, kind, paypal_invoice_id, invoice_number, status, invoice_url,"
            " amount_usd, created_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, q["provider_id"], "invoice", iid, inv.get("invoice_number"), "SENT", invoice_url,
             req["amount_usd"], iso(self.clock()), json.dumps(sent.get("raw") or sent, default=str)),
        )
        self.conn.execute("UPDATE providers SET usd_capacity_available = usd_capacity_available - ? WHERE id=?",
                          (req["amount_usd"], q["provider_id"]))
        self.conn.commit()
        self._to(rid, S.PAYPAL_ORDER_CREATED, payload={"paypal_invoice_id": iid, "kind": "invoice"})
        self._to(rid, S.AWAITING_PROVIDER_APPROVAL)
        return StepResult(True, S.AWAITING_PROVIDER_APPROVAL, "Factura enviada al proveedor en PayPal.",
                          None, {"paypal_invoice_id": iid, "invoice_url": invoice_url})

    def check_invoice_paid(self, rid: int) -> StepResult:
        """Verifica si la factura fue pagada en PayPal (por polling o webhook)."""
        req = self._req(rid)
        o = self._row("SELECT * FROM paypal_orders WHERE request_id=? AND kind='invoice' ORDER BY id DESC LIMIT 1", rid)
        if not o:
            raise ValueError("no hay factura registrada para esta solicitud")
        iid = o["paypal_invoice_id"]
        try:
            info = self._paypal_call(rid, lambda: self.gw.get_invoice(iid))
            if info["status"] == "PAID":
                self.conn.execute("UPDATE paypal_orders SET status='PAID', captured_at=? WHERE id=?",
                                  (iso(self.clock()), o["id"]))
                merchant = self._row("SELECT delivery_type FROM merchants WHERE id=?", req["merchant_id"])
                self.conn.execute("INSERT INTO deliveries (request_id, delivery_type, deadline_at) VALUES (?,?,?)",
                                  (rid, merchant["delivery_type"],
                                   iso(self.clock() + timedelta(hours=self.s.delivery_deadline_hours))))
                self.conn.commit()
                self._to(rid, S.PAID, actor="paypal_webhook", payload={"invoice_id": iid})
                self._to(rid, S.AWAITING_DELIVERY)
                return StepResult(True, S.AWAITING_DELIVERY, "Factura pagada. Esperando entrega.", None,
                                  {"invoice_id": iid})
            else:
                return StepResult(False, req["state"], f"Factura en estado {info['status']}, esperando pago del proveedor.",
                                  "WAITING_INVOICE_PAYMENT")
        except StepFailure as sf:
            return self._apply_failure(rid, sf.resolution)

    # ───────────────────────── 4. Entrega y seguimiento ─────────────────────────
    def record_delivery(self, rid: int, *, tracking_number: str | None = None, carrier: str | None = None,
                        proof: str | None = None, delivered: bool = False) -> StepResult:
        d = self._row("SELECT * FROM deliveries WHERE request_id=?", rid)
        o = self._row("SELECT * FROM paypal_orders WHERE request_id=? ORDER BY id DESC LIMIT 1", rid)
        if d["delivery_type"] == "physical" and tracking_number:
            try:
                self._paypal_call(rid, lambda: self.gw.add_tracking(
                    capture_id=o["capture_id"], tracking_number=tracking_number, carrier=carrier or "OTHER"))
            except StepFailure as sf:
                res = sf.resolution
                if res.code == "E_PP_UNPROCESSABLE":
                    res = resolve("E_TRACKING_INVALID")
                return self._apply_failure(rid, res)
        proof_hash = hashlib.sha256(proof.encode()).hexdigest()[:16] if proof else None
        status = "delivered" if delivered else "shipped"
        self.conn.execute(
            "UPDATE deliveries SET tracking_number=COALESCE(?,tracking_number), carrier=COALESCE(?,carrier),"
            " proof_hash=COALESCE(?,proof_hash), status=?, delivered_at=? WHERE request_id=?",
            (tracking_number, carrier, proof_hash, status, iso(self.clock()) if delivered else None, rid))
        self.conn.commit()
        log_event(self.conn, rid, "delivery_recorded", actor="provider", payload={"status": status},
                  now=self.clock())
        if delivered:
            self._to(rid, S.DELIVERED)
        return StepResult(True, get_state(self.conn, rid), "Entrega registrada.", None, {"status": status})

    def confirm_receipt(self, rid: int, *, actor: str = "customer") -> StepResult:
        self._to(rid, S.COMPLETED, actor=actor, event_type="completed")
        q = self._row("SELECT provider_id FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)", rid)
        self.conn.execute("UPDATE providers SET completed_orders=completed_orders+1, last_active_at=? WHERE id=?",
                          (iso(self.clock()), q["provider_id"]))
        self.conn.commit()
        return StepResult(True, S.COMPLETED, "¡Compra completada!")

    # ───────────────────────── 5. Compensación y failover ─────────────────────────
    def compensate(self, rid: int, *, refund_paypal: bool, res: Resolution) -> StepResult:
        """Deshace lo hecho: reembolso PayPal (si hubo captura) y devolución en VES."""
        if get_state(self.conn, rid) != S.REFUND_VES_PENDING:
            self._to(rid, S.REFUND_VES_PENDING, event_type="compensation_started", payload={"code": res.code})
        o = self._row("SELECT * FROM paypal_orders WHERE request_id=? ORDER BY id DESC LIMIT 1", rid)
        if refund_paypal and o and o["capture_id"] and o["status"] == "COMPLETED":
            try:
                self._paypal_call(rid, lambda: self.gw.refund_capture(
                    o["capture_id"], amount_usd=o["amount_usd"], note=f"Reembolso solicitud {rid}"))
                self.conn.execute("UPDATE paypal_orders SET status='REFUNDED' WHERE id=?", (o["id"],))
            except StepFailure as sf:
                return self._escalate(rid, sf.resolution)
        if o and o["status"] in ("CREATED", "APPROVED", "REFUNDED", "COMPLETED"):
            self.conn.execute("UPDATE providers SET usd_capacity_available = usd_capacity_available + ? WHERE id=?",
                              (o["amount_usd"], o["provider_id"]))      # liberar reserva
        pay = self._row("SELECT * FROM ves_payments WHERE request_id=? ORDER BY id DESC LIMIT 1", rid)
        amount = pay["amount_ves"] or 0.0
        if pay["status"] == "confirmed":
            if not self.ves_refunder(rid, amount):
                return self._escalate(rid, resolve("E_VES_REFUND_FAILED"))
            self.conn.execute("UPDATE ves_payments SET status='refunded', refunded_at=? WHERE id=?",
                              (iso(self.clock()), pay["id"]))
        self.conn.commit()
        self._to(rid, S.REFUNDED, event_type="refunded", payload={"code": res.code})
        return StepResult(False, S.REFUNDED, res.user_msg_es, res.code, {"refunded_ves": amount})

    def _chain_depth(self, rid: int) -> int:
        depth, row = 0, self._req(rid)
        while row["parent_request_id"]:
            depth += 1
            row = self._req(row["parent_request_id"])
        return depth

    def _failover(self, rid: int, res: Resolution) -> StepResult:
        """El proveedor falló: devolver Bs., penalizarlo y reabrir con el siguiente (cliente vuelve a pagar)."""
        req = self._req(rid)
        q = self._row("SELECT provider_id FROM quotes WHERE id=?", req["selected_quote_id"])
        self.conn.execute("UPDATE providers SET failed_orders=failed_orders+1 WHERE id=?", (q["provider_id"],))
        self.conn.commit()
        result = self.compensate(rid, refund_paypal=False, res=res)
        if get_state(self.conn, rid) != S.REFUNDED:
            return result
        if self._chain_depth(rid) >= self.s.max_provider_failovers:
            result.message_es += " Alcanzamos el máximo de reintentos con proveedores; contacta soporte."
            return result
        excluded = json.loads(req["excluded_providers"]) + [q["provider_id"]]
        new = self.create_request(req["customer_id"], req["item_id"], req["customer_bank"],
                                  f"{req['idempotency_key']}:fo{len(excluded)}",
                                  parent_request_id=rid, excluded=excluded)
        result.data["reopened_request_id"] = new.data["request_id"]
        return result

    # ───────────────────────── 6. Timeouts (lo llama un scheduler cada minuto) ─────────────────────────
    def check_timeouts(self) -> list[tuple[int, str, str]]:
        now = iso(self.clock())
        rows = self.conn.execute(
            "SELECT id, state FROM purchase_requests WHERE state_deadline_at IS NOT NULL AND state_deadline_at <= ?",
            (now,)).fetchall()
        acted = []
        for r in rows:
            rid, st = r["id"], r["state"]
            if st == S.QUOTED:
                self._to(rid, S.EXPIRED, event_type="timeout")
                acted.append((rid, st, "expired"))
            elif st == S.AWAITING_VES:
                res = self._apply_failure(rid, resolve("E_VES_TIMEOUT"))
                acted.append((rid, st, res.code))
            elif st == S.AWAITING_PROVIDER_APPROVAL:
                res = self._apply_failure(rid, resolve("E_PP_NOT_APPROVED"))
                acted.append((rid, st, res.code))
            elif st == S.AWAITING_DELIVERY:
                res = self._apply_failure(rid, resolve("E_DELIVERY_TIMEOUT"))
                acted.append((rid, st, res.code))
            elif st == S.DELIVERED:
                self.confirm_receipt(rid, actor="system")
                acted.append((rid, st, "auto_completed"))
        return acted

    # ───────────────────────── 7. Disputas ─────────────────────────
    def open_dispute(self, rid: int, paypal_dispute_id: str, reason: str, respond_by: datetime) -> StepResult:
        req = self._req(rid)
        self._to(rid, S.DISPUTED, actor="paypal_webhook", payload={"dispute": paypal_dispute_id, "reason": reason})
        d = self._row("SELECT * FROM deliveries WHERE request_id=?", rid)
        strength = policy.proof_strength(
            delivery_status=d["status"] if d else None, tracking_number=d["tracking_number"] if d else None,
            proof_hash=d["proof_hash"] if d else None)
        hours_left = (respond_by - self.clock()).total_seconds() / 3600
        allowed, forced = policy.allowed_dispute_actions(
            amount_usd=req["amount_usd"], strength=strength, hours_left=hours_left, settings=self.s)
        if forced:
            self._log_error(rid, resolve(forced), 0, {"hours_left": hours_left, "amount": req["amount_usd"]})

        ctx = {"reason": reason, "amount_usd": req["amount_usd"], "proof_strength": strength,
               "hours_left": round(hours_left, 1), "tracking_number": d["tracking_number"] if d else None,
               "delivery_status": d["status"] if d else None, "proof_hash": d["proof_hash"] if d else None}
        decision, by, rationale, esc_code = self._decide_dispute(rid, ctx, allowed, strength)

        cur = self.conn.execute(
            "INSERT INTO disputes (request_id, paypal_dispute_id, reason, amount_usd, proof_strength, decision,"
            " decided_by, rationale_es, respond_by, opened_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, paypal_dispute_id, reason, req["amount_usd"], strength, decision, by, rationale,
             iso(respond_by), iso(self.clock())))
        did = cur.lastrowid
        self.conn.commit()

        try:
            if decision == policy.CONTEST:
                notes = f"Entrega verificable. {rationale}"
                self._paypal_call(rid, lambda: self.gw.provide_evidence(
                    paypal_dispute_id, notes=notes, tracking_number=ctx["tracking_number"]))
                self.conn.execute("INSERT INTO dispute_evidence (dispute_id, kind, content) VALUES (?,?,?)",
                                  (did, "PROOF_OF_FULFILLMENT", json.dumps(ctx)))
                self.conn.commit()
                self._to(rid, S.DISPUTE_CONTESTED, actor="agent" if by == "agent" else "system")
                return StepResult(True, S.DISPUTE_CONTESTED, resolve("E_DISPUTE_STRONG_EVIDENCE").user_msg_es,
                                  None, {"decision": decision, "decided_by": by})
            if decision == policy.ACCEPT:
                self._paypal_call(rid, lambda: self.gw.accept_claim(paypal_dispute_id, note=rationale))
                self.conn.execute("UPDATE disputes SET outcome='refunded', status='closed', closed_at=? WHERE id=?",
                                  (iso(self.clock()), did))
                self._mark_provider_dispute(req)
                res = resolve("E_DISPUTE_NO_EVIDENCE")
                out = self.compensate(rid, refund_paypal=False, res=res)   # PayPal ya reembolsa al aceptar
                out.data["decision"] = decision
                return out
        except StepFailure as sf:
            return self._apply_failure(rid, sf.resolution)

        res = resolve(forced or esc_code or "E_AGENT_ESCALATED")
        return self._escalate(rid, res, note=json.dumps(ctx))

    def _decide_dispute(self, rid: int, ctx: dict, allowed: set[str], strength: str
                        ) -> tuple[str, str, str, str | None]:
        """-> (decisión, quién decidió, justificación, código de escalamiento si aplica)."""
        if allowed == {policy.HUMAN}:
            return policy.HUMAN, "rule", "Fuera del alcance autónomo del agente.", None
        if self.brain:
            try:
                out = self.brain.decide_dispute(ctx, allowed)
                if out["decision"] in allowed:
                    return out["decision"], "agent", out.get("rationale_es", ""), "E_AGENT_ESCALATED"
                res = resolve("E_GUARDRAIL_BLOCKED")
                self._log_error(rid, res, 0, {"proposed": out["decision"], "allowed": sorted(allowed)})
                return policy.HUMAN, "rule", "El agente propuso una acción no permitida.", "E_GUARDRAIL_BLOCKED"
            except Exception as e:                                  # LLM caído -> reglas
                self._log_error(rid, resolve("E_UNKNOWN"), 0, {"brain_error": str(e)})
        return policy.rule_based_decision(allowed, strength), "rule", "Decisión por reglas de respaldo.", None

    def _mark_provider_dispute(self, req: sqlite3.Row) -> None:
        q = self._row("SELECT provider_id FROM quotes WHERE id=?", req["selected_quote_id"])
        self.conn.execute("UPDATE providers SET disputed_orders=disputed_orders+1 WHERE id=?", (q["provider_id"],))
        self.conn.commit()

    def resolve_dispute_outcome(self, rid: int, outcome: str) -> StepResult:
        """Webhook CUSTOMER.DISPUTE.RESOLVED: outcome = 'won' | 'lost'."""
        self.conn.execute("UPDATE disputes SET outcome=?, status='closed', closed_at=? WHERE request_id=?",
                          (outcome, iso(self.clock()), rid))
        if outcome == "won":
            self._to(rid, S.COMPLETED, actor="paypal_webhook", event_type="dispute_won")
            return StepResult(True, S.COMPLETED, "Ganamos la disputa.")
        self._mark_provider_dispute(self._req(rid))
        return self.compensate(rid, refund_paypal=False, res=resolve("E_DISPUTE_NO_EVIDENCE"))
