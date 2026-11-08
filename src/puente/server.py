"""Receptor de Webhooks de PayPal y Scheduler de Timeouts (FastAPI).

Flujo event-driven:
  - CHECKOUT.ORDER.APPROVED -> captura automática sin esperar al humano.
  - CUSTOMER.DISPUTE.CREATED -> apertura de disputa con guardrails de código y LLM.
  - CUSTOMER.DISPUTE.RESOLVED -> resolución automática ganada / perdida.
  - Idempotencia nativa: paypal_event_id UNIQUE en tabla webhook_events.
  - Scheduler asíncrono: loop en background corriendo check_timeouts() cada 60 s.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .cli import build, seed
from .concierge import ChatLLM, Concierge, make_llm
from .config import Settings
from .db import init_db, iso, parse_iso, utcnow
from .orchestrator import Orchestrator
from .state_machine import State as S, state_timeout_minutes

logger = logging.getLogger("puente.server")


def _summarize_tool_step(name: str, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        return {"name": name, "summary": str(result["error"]), "ok": False}
    if name == "list_catalog":
        items = result.get("items") or []
        return {"name": name, "summary": f"{len(items)} productos disponibles", "ok": True}
    if name == "get_quotes":
        offers = result.get("offers") or []
        filtered = result.get("providers_filtered_out", 0)
        return {
            "name": name,
            "summary": f"{result.get('item', 'Producto')}, banco {args.get('bank', '')}: {len(offers)} ofertas, {filtered} proveedores descartados",
            "ok": True,
        }
    if name == "propose_purchase":
        return {
            "name": name,
            "summary": f"oferta {args.get('rank', 1)}, pendiente de confirmación del usuario",
            "ok": True,
        }
    if name == "check_status":
        return {"name": name, "summary": f"estado {result.get('state', '')}", "ok": True}
    if name == "report_payment":
        return {
            "name": name,
            "summary": f"referencia {args.get('bank_reference', '')} ({result.get('state', '')})",
            "ok": True,
        }
    return {"name": name, "summary": "ok", "ok": True}


def _extract_turn_trace(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Extrae los ToolStep ejecutados en los mensajes nuevos de este turno y si se llamó get_quotes."""
    call_map: dict[str, tuple[str, dict[str, Any]]] = {}
    steps: list[dict[str, Any]] = []
    got_quotes = False
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                call_map[tc.get("id", "")] = (name, args if isinstance(args, dict) else {})
        elif m.get("role") == "tool":
            tc_id = m.get("tool_call_id", "")
            name, args = call_map.get(tc_id, ("tool", {}))
            try:
                res = json.loads(m.get("content") or "{}")
            except Exception:
                res = {}
            if not isinstance(res, dict):
                res = {}
            if name == "get_quotes" and "error" not in res:
                got_quotes = True
            steps.append(_summarize_tool_step(name, args, res))
    return steps, got_quotes


def _format_offers(conn: sqlite3.Connection, quotes: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    if not quotes:
        return None
    best = min(x["total_ves"] for x in quotes)
    offers = []
    for x in quotes:
        p = conn.execute(
            "SELECT completed_orders c, disputed_orders d, avg_settle_minutes m FROM providers WHERE id=?",
            (x["provider_id"],),
        ).fetchone()
        offers.append({
            "rank": x["rank"],
            "provider": x["display_name"],
            "totalVes": x["total_ves"],
            "rateVesPerUsd": x["rate_ves_per_usd"],
            "pctMoreThanCheapest": round((x["total_ves"] / best - 1) * 100, 1),
            "completedOps": p["c"] if p else 0,
            "disputedOps": p["d"] if p else 0,
            "avgMinutesToConfirm": p["m"] if p else 15,
        })
    return offers


def _format_pending(pending: dict[str, Any] | None) -> dict[str, Any] | None:
    if not pending:
        return None
    return {
        "rank": pending["rank"],
        "provider": pending["provider"],
        "totalVes": pending["total_ves"],
        "item": pending["item"],
        "amountUsd": pending["amount_usd"],
    }


EVENT_LABELS: dict[str, str] = {
    "request_created": "Solicitud creada",
    "quoted": "Cotizaciones generadas y rankeadas",
    "quote_selected": "Proveedor seleccionado por el cliente",
    "provider_locked": "El proveedor aprobó el pago en PayPal",
    "ves_reported": "El cliente envió la referencia del pago",
    "ref_mismatch": "El proveedor no encontró la referencia",
    "ves_confirmed": "El proveedor confirmó que recibió los bolívares",
    "delivery_recorded": "El comercio registró la entrega",
    "completed": "Operación completada",
    "escrow_cancelled": "Custodia liberada en PayPal",
}

STATE_LABELS: dict[str, str] = {
    "AWAITING_PROVIDER_APPROVAL": "Solicitud creada y orden de PayPal generada",
    "USD_ESCROW_LOCKED": "Fondos bloqueados en PayPal Escrow",
    "AWAITING_VES": "Esperando pago en bolívares del cliente",
    "VES_REPORTED": "Referencia de Pago Móvil reportada",
    "VES_CONFIRMED": "El proveedor confirmó que recibió los bolívares",
    "PAID": "Pago capturado en PayPal y enviado al comercio",
    "AWAITING_DELIVERY": "Esperando la entrega del comercio",
    "DELIVERED": "Entrega realizada por el comercio",
    "COMPLETED": "Compra finalizada con éxito",
    "EXPIRED": "Solicitud cerrada sin completarse",
}


def create_app(
    conn: sqlite3.Connection | None = None,
    orch: Orchestrator | None = None,
    *,
    llm: ChatLLM | None = None,
    enable_scheduler: bool = True,
    scheduler_interval_seconds: int = 60,
    settings: Settings | None = None,
) -> FastAPI:
    s = settings or Settings()

    # Si no se inyecta conexión/orquestador, usamos el builder estándar
    if conn is None or orch is None:
        c, o, _, _ = build(settings=s)
        conn = conn or c
        orch = orch or o

    sessions: dict[str, Concierge] = {}
    llm_instance: ChatLLM | None = llm

    def _get_session(session_id: str) -> Concierge:
        nonlocal llm_instance
        if session_id not in sessions:
            if llm_instance is None:
                llm_instance = make_llm()
            sessions[session_id] = Concierge(
                orch, conn, llm_instance, s, show_provider_messages=False, out=lambda _: None
            )
        return sessions[session_id]

    scheduler_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal scheduler_task
        if enable_scheduler:
            async def _loop():
                logger.info(f"Iniciando scheduler de timeouts (intervalo: {scheduler_interval_seconds}s)")
                while True:
                    try:
                        acted = orch.check_timeouts()
                        if acted:
                            logger.info(f"Scheduler timeouts aplicados: {acted}")
                    except Exception as e:
                        logger.error(f"Error en scheduler: {e}", exc_info=True)
                    await asyncio.sleep(scheduler_interval_seconds)

            scheduler_task = asyncio.create_task(_loop())
        yield
        if scheduler_task:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="PUENTE Webhook & Event Engine", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _active_request_id(c: Concierge) -> int | None:
        if c.rid is None:
            return None
        row = conn.execute(
            "SELECT selected_quote_id, state FROM purchase_requests WHERE id=?", (c.rid,)
        ).fetchone()
        if not row or row["selected_quote_id"] is None:
            return None
        return c.rid

    @app.post("/chat")
    def chat_turn(payload: dict = Body(...)):
        session_id = str(payload.get("session_id") or "default")
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Falta el campo 'text'.")
        c = _get_session(session_id)
        start_idx = len(c.messages)
        reply = c.turn(text)
        new_msgs = c.messages[start_idx:]
        trace, got_quotes = _extract_turn_trace(new_msgs)
        offers = _format_offers(conn, c.quotes) if got_quotes else None
        pending = _format_pending(c.pending)
        return {
            "reply": reply,
            "trace": trace,
            "offers": offers,
            "pending": pending,
            "requestId": _active_request_id(c),
        }

    @app.post("/chat/{session_id}/confirm")
    def chat_confirm(session_id: str):
        c = _get_session(session_id)
        if not c.pending:
            return {
                "reply": "No hay ninguna compra pendiente de confirmar.",
                "trace": [],
                "offers": None,
                "pending": None,
                "requestId": _active_request_id(c),
            }
        res = c.confirm_pending()
        reply = (
            "Orden creada. Le avisé al proveedor para que bloquee el pago en PayPal. "
            "Cuando lo haga te muestro los datos para pagar."
            if res.ok
            else res.message_es
        )
        c.messages.append({"role": "assistant", "content": reply})
        trace = [
            {
                "name": "confirm_purchase",
                "summary": "ejecutado por el código tras el sí del usuario" if res.ok else res.message_es,
                "ok": res.ok,
            },
        ]
        return {
            "reply": reply,
            "trace": trace,
            "offers": None,
            "pending": None,
            "requestId": c.rid,
        }

    @app.post("/chat/{session_id}/cancel")
    def chat_cancel(session_id: str):
        c = _get_session(session_id)
        c.cancel_pending()
        reply = "Cancelado. No se movió nada."
        c.messages.append({"role": "assistant", "content": reply})
        return {
            "reply": reply,
            "trace": [],
            "offers": None,
            "pending": None,
            "requestId": _active_request_id(c),
        }

    @app.get("/p2p/requests/{request_id}/view")
    def p2p_request_view(request_id: int):
        req = conn.execute("SELECT * FROM purchase_requests WHERE id=?", (request_id,)).fetchone()
        if not req:
            raise HTTPException(status_code=404, detail="Solicitud no encontrada")
        item = conn.execute("SELECT * FROM catalog_items WHERE id=?", (req["item_id"],)).fetchone()
        merchant = conn.execute("SELECT * FROM merchants WHERE id=?", (req["merchant_id"],)).fetchone()

        q = None
        if req["selected_quote_id"]:
            q = conn.execute("SELECT * FROM quotes WHERE id=?", (req["selected_quote_id"],)).fetchone()
        if not q:
            q = conn.execute(
                "SELECT * FROM quotes WHERE request_id=? ORDER BY rank ASC LIMIT 1", (request_id,)
            ).fetchone()

        prov = (
            conn.execute("SELECT * FROM providers WHERE id=?", (q["provider_id"],)).fetchone()
            if q
            else None
        )
        po = conn.execute(
            "SELECT * FROM paypal_orders WHERE request_id=? ORDER BY id DESC LIMIT 1", (request_id,)
        ).fetchone()
        vp = conn.execute(
            "SELECT * FROM ves_payments WHERE request_id=? ORDER BY id DESC LIMIT 1", (request_id,)
        ).fetchone()

        raw_state = req["state"]
        ui_state = "AWAITING_VES" if raw_state == "USD_ESCROW_LOCKED" else raw_state

        timeout_min = state_timeout_minutes(raw_state, s)
        deadline: str | None = None
        if timeout_min:
            base_dt = parse_iso(req["updated_at"])
            deadline = iso(base_dt + timedelta(minutes=timeout_min))

        payment = None
        if raw_state not in ("CREATED", "QUOTED", "PAYPAL_ORDER_CREATED", "AWAITING_PROVIDER_APPROVAL") and req["selected_quote_id"]:
            ticket = orch.get_pago_movil_ticket(request_id)
            payment = {
                "bank": ticket["bank_code"],
                "phone": re.sub(r"\D", "", ticket["phone"]),
                "idDocument": ticket["id_doc"],
                "holder": ticket["holder_name"],
                "totalVes": float(ticket["total_ves"]),
            }

        report = None
        if vp and vp["reference"]:
            report = {
                "reference": vp["reference"],
                "amountVes": float(vp["amount_ves"]),
                "payerBank": vp["payer_bank"],
                "payerPhone": vp["payer_phone"],
            }

        outbox_rows = conn.execute(
            "SELECT id, to_role, template, body, created_at FROM outbox WHERE request_id=? ORDER BY id ASC",
            (request_id,),
        ).fetchall()
        inbox = [
            {
                "id": r["id"],
                "role": r["to_role"] if r["to_role"] in ("customer", "provider", "operator") else "operator",
                "template": r["template"],
                "body": r["body"],
                "createdAt": r["created_at"],
            }
            for r in outbox_rows
        ]

        event_rows = conn.execute(
            "SELECT ts, event_type, to_state FROM events WHERE request_id=? ORDER BY id ASC",
            (request_id,),
        ).fetchall()
        timeline: list[dict[str, str]] = []
        seen_labels: set[str] = set()
        for e in event_rows:
            et = e["event_type"]
            to_st = e["to_state"]
            label = EVENT_LABELS.get(et) or (STATE_LABELS.get(to_st) if to_st else None)
            if label and label not in seen_labels:
                seen_labels.add(label)
                timeline.append({"at": e["ts"], "label": label})

        provider_id = prov["id"] if prov else 1
        return {
            "requestId": request_id,
            "state": ui_state,
            "item": item["title"] if item else "Producto",
            "amountUsd": float(req["amount_usd"]),
            "merchant": merchant["name"] if merchant else "Comercio",
            "totalVes": float(q["total_ves"]) if q else 0.0,
            "providerName": prov["display_name"] if prov else "Sin asignar",
            "lockMode": "authorize" if (po and po["authorization_id"]) else "approve",
            "deadline": deadline,
            "approveUrl": po["approve_url"] if po else None,
            "payment": payment,
            "report": report,
            "paypalOrderId": po["paypal_order_id"] if po else None,
            "inbox": inbox,
            "events": timeline,
            "demoProviderToken": f"tok-demo-provider-{provider_id}",
        }

    @app.post("/demo/provider/approve/{request_id}")
    def demo_provider_approve(request_id: int):
        po = conn.execute(
            "SELECT paypal_order_id FROM paypal_orders WHERE request_id=? ORDER BY id DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        if not po:
            raise HTTPException(status_code=404, detail="No hay orden de PayPal para esta solicitud.")
        if hasattr(orch.gw, "approve"):
            orch.gw.approve(po["paypal_order_id"])
        res = orch.on_provider_locked(request_id, force_demo=True)
        if not res.ok:
            raise HTTPException(status_code=400, detail=res.message_es)
        return {"status": "ok", "state": res.state}

    @app.post("/p2p/requests/{request_id}/report")
    def p2p_report_ves(request_id: int, payload: dict = Body(...)):
        ref = str(payload.get("bank_reference") or payload.get("reference") or "").strip()
        amount = payload.get("amount_ves")
        payer_bank = payload.get("payer_bank")
        payer_phone = payload.get("payer_phone")
        if not ref or amount is None:
            raise HTTPException(status_code=400, detail="Faltan 'bank_reference' y 'amount_ves'.")
        res = orch.report_ves_payment(
            request_id,
            bank_reference=ref,
            amount_ves=float(amount),
            payer_bank=payer_bank,
            payer_phone=payer_phone,
        )
        if not res.ok:
            raise HTTPException(status_code=400, detail=res.message_es)
        return {"status": "ok", "state": res.state}

    @app.post("/p2p/requests/{request_id}/provider/confirm")
    def p2p_provider_confirm(request_id: int):
        req = conn.execute("SELECT selected_quote_id FROM purchase_requests WHERE id=?", (request_id,)).fetchone()
        if not req or not req["selected_quote_id"]:
            raise HTTPException(status_code=404, detail="Solicitud o proveedor no encontrado.")
        q = conn.execute("SELECT provider_id FROM quotes WHERE id=?", (req["selected_quote_id"],)).fetchone()
        res = orch.provider_confirm_ves(request_id, q["provider_id"])
        if not res.ok:
            raise HTTPException(status_code=400, detail=res.message_es)
        return {"status": "ok", "state": res.state}

    @app.post("/p2p/requests/{request_id}/provider/reject")
    def p2p_provider_reject(request_id: int, payload: dict = Body(...)):
        reason = str(payload.get("reason") or "not_received")
        res = orch.provider_reject_ves(request_id, reason)
        if not res.ok:
            raise HTTPException(status_code=400, detail=res.message_es)
        return {"status": "ok", "state": res.state}

    @app.post("/p2p/requests/{request_id}/provider/sms")
    def p2p_provider_sms(request_id: int, payload: dict = Body(...)):
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Pega el texto del SMS del banco.")
        vp = conn.execute(
            "SELECT reference FROM ves_payments WHERE request_id=? ORDER BY id DESC LIMIT 1", (request_id,)
        ).fetchone()
        if vp and vp["reference"] and vp["reference"] not in text:
            raise HTTPException(
                status_code=400,
                detail=f"El SMS no contiene la referencia reportada ({vp['reference']}).",
            )
        req = conn.execute("SELECT selected_quote_id FROM purchase_requests WHERE id=?", (request_id,)).fetchone()
        q = conn.execute("SELECT provider_id FROM quotes WHERE id=?", (req["selected_quote_id"],)).fetchone()
        res = orch.provider_confirm_ves(request_id, q["provider_id"])
        if not res.ok:
            raise HTTPException(status_code=400, detail=res.message_es)
        return {"status": "ok", "state": res.state}

    @app.post("/demo/reset")
    def demo_reset():
        sessions.clear()
        if hasattr(orch.gw, "orders"):
            orch.gw.orders.clear()
        if hasattr(orch.gw, "invoices"):
            orch.gw.invoices.clear()
        if hasattr(orch.gw, "_n"):
            orch.gw._n = 0
        conn.execute("PRAGMA foreign_keys = OFF;")
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for tbl in tables:
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.commit()
        init_db(conn)
        seed(conn)
        return {"status": "ok"}

    @app.get("/health")
    def health():
        counts = conn.execute(
            "SELECT state, COUNT(*) as c FROM purchase_requests GROUP BY state"
        ).fetchall()
        return {
            "status": "healthy",
            "time": iso(utcnow()),
            "requests_by_state": {r["state"]: r["c"] for r in counts},
        }

    @app.get("/requests/{request_id}")
    def get_request(request_id: int):
        req = conn.execute("SELECT * FROM purchase_requests WHERE id=?", (request_id,)).fetchone()
        if not req:
            raise HTTPException(status_code=404, detail="Solicitud no encontrada")
        quotes = conn.execute("SELECT * FROM quotes WHERE request_id=?", (request_id,)).fetchall()
        events = conn.execute(
            "SELECT ts, actor, event_type, from_state, to_state, payload FROM events WHERE request_id=? ORDER BY id ASC",
            (request_id,),
        ).fetchall()
        return {
            "request": dict(req),
            "quotes": [dict(q) for q in quotes],
            "history": [dict(e) for e in events],
        }

    @app.post("/p2p/intent")
    def p2p_intent(payload: dict = Body(...)):
        """María: entrada en lenguaje natural procesada con IA -> solicitud + cotizaciones rankeadas con explicación."""
        text = payload.get("text", "")
        if not text:
            raise HTTPException(status_code=400, detail="Falta el campo 'text' con la solicitud.")
        cid = payload.get("customer_id", 1)
        bank = payload.get("bank")
        key = payload.get("idempotency_key") or f"intent-{int(time.time() * 1000)}"

        res = orch.create_request_from_text(cid, text, key, bank=bank)
        if not res.ok:
            raise HTTPException(status_code=400, detail=res.message_es)
        rid = res.data["request_id"]
        q_res = orch.quote(rid)
        if not q_res.ok:
            raise HTTPException(status_code=400, detail=q_res.message_es)

        return {
            "request_id": rid,
            "state": q_res.state,
            "parsed_intent": res.data.get("parsed_intent"),
            "matched_item": res.data.get("matched_item"),
            "fx_rate": q_res.data.get("fx_rate"),
            "quotes": q_res.data.get("quotes"),
            "ai_explanation": q_res.data.get("ai_explanation"),
        }

    @app.post("/p2p/requests/{request_id}/select")
    def p2p_select_and_escrow(request_id: int, payload: dict = Body(default={})):
        """María elige una oferta -> PUENTE crea la orden de retención en PayPal (Escrow)."""
        rank = payload.get("rank", 1)
        sel = orch.select_quote(request_id, rank=rank)
        if not sel.ok:
            raise HTTPException(status_code=400, detail=sel.message_es)
        escrow = orch.create_escrow_order(request_id)
        if not escrow.ok:
            raise HTTPException(status_code=400, detail=escrow.message_es)
        return {
            "request_id": request_id,
            "state": escrow.state,
            "paypal_order_id": escrow.data.get("paypal_order_id"),
            "approve_url": escrow.data.get("approve_url"),
        }

    @app.get("/p2p/requests/{request_id}/ticket")
    def p2p_get_ticket(request_id: int):
        """María obtiene los datos del Pago Móvil (solo si los dólares ya están retenidos)."""
        req = conn.execute("SELECT state FROM purchase_requests WHERE id=?", (request_id,)).fetchone()
        if not req:
            raise HTTPException(status_code=404, detail="Solicitud no encontrada")
        ticket = orch.get_pago_movil_ticket(request_id)
        return {"ticket": ticket, "state": req["state"]}

    @app.post("/p2p/requests/{request_id}/lock")
    def p2p_lock_usd(request_id: int):
        """Carlos: una vez aprobada la orden en PayPal, PUENTE autoriza y retiene los USD en custodia."""
        res = orch.lock_usd_escrow(request_id)
        if not res.ok:
            raise HTTPException(status_code=400, detail=res.message_es)
        return {"status": "ok", "state": res.state, "ticket": res.data.get("ticket")}

    @app.post("/p2p/requests/{request_id}/confirm-ves")
    def p2p_confirm_ves(request_id: int, payload: dict = Body(...)):
        """María: envía comprobante y referencia del Pago Móvil."""
        ref = payload.get("reference")
        amount = payload.get("amount_ves")
        if not ref or not amount:
            raise HTTPException(status_code=400, detail="Faltan 'reference' y 'amount_ves'.")
        res = orch.confirm_ves(request_id, ref, float(amount))
        if not res.ok:
            raise HTTPException(status_code=400, detail=res.message_es)
        return {"status": "ok", "state": res.state, "reference": ref}

    @app.post("/p2p/requests/{request_id}/release")
    def p2p_release(request_id: int):
        """Carlos: confirma que recibió los Bolívares en su banco -> PUENTE libera y captura los USD."""
        res = orch.provider_release_and_capture(request_id)
        if not res.ok:
            raise HTTPException(status_code=400, detail=res.message_es)
        orch.record_delivery(request_id, proof="LICENCIA-ONLINE-2026", delivered=True)
        orch.confirm_receipt(request_id)
        return {"status": "ok", "state": "COMPLETED", "capture_id": res.data.get("capture_id")}

    @app.post("/scheduler/tick")
    def trigger_scheduler():
        """Trigger manual para check_timeouts() (útil para cron externo o tests)."""
        acted = orch.check_timeouts()
        return {"status": "ok", "acted": acted}

    @app.post("/webhooks/paypal")
    async def handle_paypal_webhook(request: Request):
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

        event_id = body.get("id")
        event_type = body.get("event_type", "UNKNOWN")
        resource = body.get("resource") or {}

        if not event_id:
            raise HTTPException(status_code=400, detail="Missing 'id' in webhook payload")

        # 1. Idempotencia: Verificar si el evento ya fue registrado
        try:
            conn.execute(
                "INSERT INTO webhook_events (paypal_event_id, event_type, payload) VALUES (?,?,?)",
                (event_id, event_type, json.dumps(body, default=str)),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Reintento de PayPal: responder 200 OK inmediatamente sin duplicar operaciones
            return JSONResponse(
                content={"status": "ignored", "reason": "duplicate_event", "event_id": event_id},
                status_code=200,
            )

        handled_action = None
        details: dict[str, Any] = {}

        try:
            # 2. Despacho según el tipo de evento
            if event_type == "CHECKOUT.ORDER.APPROVED":
                order_id = resource.get("id")
                row = conn.execute(
                    "SELECT request_id FROM paypal_orders WHERE paypal_order_id=?", (order_id,)
                ).fetchone()
                if row:
                    res = orch.capture(row["request_id"])
                    handled_action = "order_captured"
                    details = {"request_id": row["request_id"], "state": res.state, "ok": res.ok}
                else:
                    handled_action = "order_not_found"

            elif event_type == "CUSTOMER.DISPUTE.CREATED":
                dispute_id = resource.get("dispute_id") or resource.get("id")
                reason = resource.get("reason", "MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
                due_date_raw = resource.get("response_due_date")
                due_date = parse_iso(due_date_raw.replace("Z", "")) if due_date_raw else (utcnow() + timedelta(hours=72))

                # Intentar ubicar request_id por capture_id en transacciones en disputa
                req_id = None
                for tx in resource.get("disputed_transactions", []):
                    cap_id = tx.get("seller_transaction_id") or tx.get("buyer_transaction_id")
                    if cap_id:
                        r = conn.execute("SELECT request_id FROM paypal_orders WHERE capture_id=?", (cap_id,)).fetchone()
                        if r:
                            req_id = r["request_id"]
                            break

                if req_id:
                    res = orch.open_dispute(req_id, dispute_id, reason, due_date)
                    handled_action = "dispute_opened"
                    details = {"request_id": req_id, "state": res.state}
                else:
                    handled_action = "disputed_request_not_found"

            elif event_type == "CUSTOMER.DISPUTE.RESOLVED":
                dispute_id = resource.get("dispute_id") or resource.get("id")
                outcome_code = (resource.get("dispute_outcome") or {}).get("outcome_code", "")
                outcome = "won" if "SELLER" in outcome_code.upper() else "lost"

                row = conn.execute("SELECT request_id FROM disputes WHERE paypal_dispute_id=?", (dispute_id,)).fetchone()
                if row:
                    res = orch.resolve_dispute_outcome(row["request_id"], outcome)
                    handled_action = "dispute_resolved"
                    details = {"request_id": row["request_id"], "outcome": outcome, "state": res.state}
                else:
                    handled_action = "dispute_record_not_found"

            elif event_type in ("INVOICING.INVOICE.PAID", "INVOICE.PAID"):
                invoice_id = resource.get("id") or resource.get("invoice", {}).get("id")
                row = conn.execute(
                    "SELECT request_id FROM paypal_orders WHERE paypal_invoice_id=?", (invoice_id,)
                ).fetchone()
                if row:
                    res = orch.check_invoice_paid(row["request_id"])
                    handled_action = "invoice_paid"
                    details = {"request_id": row["request_id"], "state": res.state, "ok": res.ok}
                else:
                    handled_action = "invoice_not_found"

            else:
                handled_action = "acknowledged_unhandled"

            # 3. Marcar evento como procesado
            conn.execute(
                "UPDATE webhook_events SET processed_at=? WHERE paypal_event_id=?",
                (iso(utcnow()), event_id),
            )
            conn.commit()

        except Exception as e:
            logger.error(f"Error procesando webhook {event_id}: {e}", exc_info=True)
            return JSONResponse(
                content={"status": "error", "event_id": event_id, "error": str(e)},
                status_code=500,
            )

        return {
            "status": "success",
            "event_id": event_id,
            "event_type": event_type,
            "action": handled_action,
            "details": details,
        }

    return app


# Instancia por defecto para correr con: uvicorn puente.server:app --port 8000
app = create_app()
