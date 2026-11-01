"""Máquina de estados de una solicitud de compra.

El LLM NUNCA cambia estados directamente: solo este módulo lo hace, validando transiciones
y dejando rastro en `events`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from .config import Settings
from .db import iso, log_event, utcnow


class State:
    CREATED = "CREATED"
    QUOTING = "QUOTING"
    QUOTED = "QUOTED"
    AWAITING_VES = "AWAITING_VES"
    VES_CONFIRMED = "VES_CONFIRMED"
    PAYPAL_ORDER_CREATED = "PAYPAL_ORDER_CREATED"
    AWAITING_PROVIDER_APPROVAL = "AWAITING_PROVIDER_APPROVAL"
    PAID = "PAID"
    AWAITING_DELIVERY = "AWAITING_DELIVERY"
    DELIVERED = "DELIVERED"
    COMPLETED = "COMPLETED"
    DISPUTED = "DISPUTED"
    DISPUTE_CONTESTED = "DISPUTE_CONTESTED"
    REFUND_VES_PENDING = "REFUND_VES_PENDING"
    REFUNDED = "REFUNDED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


S = State

TRANSITIONS: dict[str, set[str]] = {
    S.CREATED: {S.QUOTING},
    S.QUOTING: {S.QUOTED, S.FAILED},
    S.QUOTED: {S.AWAITING_VES, S.EXPIRED},
    S.AWAITING_VES: {S.VES_CONFIRMED, S.EXPIRED, S.HUMAN_REVIEW, S.REFUND_VES_PENDING},
    S.VES_CONFIRMED: {S.PAYPAL_ORDER_CREATED, S.REFUND_VES_PENDING, S.HUMAN_REVIEW},
    S.PAYPAL_ORDER_CREATED: {S.AWAITING_PROVIDER_APPROVAL, S.REFUND_VES_PENDING, S.HUMAN_REVIEW},
    S.AWAITING_PROVIDER_APPROVAL: {S.PAID, S.REFUND_VES_PENDING, S.HUMAN_REVIEW},
    S.PAID: {S.AWAITING_DELIVERY, S.HUMAN_REVIEW, S.DISPUTED},
    S.AWAITING_DELIVERY: {S.DELIVERED, S.DISPUTED, S.REFUND_VES_PENDING, S.HUMAN_REVIEW},
    S.DELIVERED: {S.COMPLETED, S.DISPUTED, S.HUMAN_REVIEW},
    S.COMPLETED: {S.DISPUTED},                      # disputas tardías (hasta 180 días en PayPal)
    S.DISPUTED: {S.DISPUTE_CONTESTED, S.REFUND_VES_PENDING, S.HUMAN_REVIEW},
    S.DISPUTE_CONTESTED: {S.COMPLETED, S.REFUND_VES_PENDING, S.HUMAN_REVIEW},
    S.REFUND_VES_PENDING: {S.REFUNDED, S.HUMAN_REVIEW},
    S.HUMAN_REVIEW: {S.REFUND_VES_PENDING, S.COMPLETED, S.FAILED, S.AWAITING_DELIVERY,
                     S.PAYPAL_ORDER_CREATED, S.DISPUTE_CONTESTED, S.VES_CONFIRMED},
    S.REFUNDED: set(),
    S.EXPIRED: set(),
    S.FAILED: set(),
}

TERMINAL = {S.REFUNDED, S.EXPIRED, S.FAILED}


def state_timeout_minutes(state: str, s: Settings) -> int | None:
    """Cuánto puede vivir una solicitud en `state` antes de que el scheduler actúe."""
    return {
        S.QUOTED: s.quote_ttl_minutes + s.quote_grace_minutes,
        S.AWAITING_VES: s.ves_confirm_timeout_min,
        S.AWAITING_PROVIDER_APPROVAL: s.provider_approval_timeout_min,
        S.AWAITING_DELIVERY: s.delivery_deadline_hours * 60,
        S.DELIVERED: s.auto_complete_hours * 60,
    }.get(state)


class InvalidTransition(Exception):
    code = "E_INVALID_TRANSITION"


def get_state(conn: sqlite3.Connection, request_id: int) -> str:
    row = conn.execute("SELECT state FROM purchase_requests WHERE id=?", (request_id,)).fetchone()
    if row is None:
        raise KeyError(f"request {request_id} not found")
    return row["state"]


def transition(
    conn: sqlite3.Connection,
    request_id: int,
    to_state: str,
    settings: Settings,
    *,
    actor: str = "system",
    event_type: str = "state_change",
    payload: dict | None = None,
    now: datetime | None = None,
) -> None:
    now = now or utcnow()
    from_state = get_state(conn, request_id)
    if to_state not in TRANSITIONS.get(from_state, set()):
        raise InvalidTransition(f"{from_state} -> {to_state} no permitido (request {request_id})")
    minutes = state_timeout_minutes(to_state, settings)
    deadline = iso(now + timedelta(minutes=minutes)) if minutes else None
    conn.execute(
        "UPDATE purchase_requests SET state=?, state_deadline_at=?, updated_at=? WHERE id=?",
        (to_state, deadline, iso(now), request_id),
    )
    log_event(conn, request_id, event_type, actor=actor, from_state=from_state, to_state=to_state,
              payload=payload, now=now)
    conn.commit()
