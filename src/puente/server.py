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
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .cli import build
from .config import Settings
from .db import iso, parse_iso, utcnow
from .orchestrator import Orchestrator

logger = logging.getLogger("puente.server")


def create_app(
    conn: sqlite3.Connection | None = None,
    orch: Orchestrator | None = None,
    *,
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
