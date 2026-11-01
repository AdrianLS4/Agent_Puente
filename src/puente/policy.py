"""Guardrails de disputas: el LLM puede ELEGIR, pero solo dentro de lo que esta política permite."""
from __future__ import annotations

from .config import Settings

CONTEST, ACCEPT, HUMAN = "contest", "accept", "human"


def proof_strength(*, delivery_status: str | None, tracking_number: str | None, proof_hash: str | None) -> str:
    """strong: entrega confirmada con tracking o comprobante digital verificable.
    weak: enviado pero sin confirmación de entrega.  none: nada."""
    if delivery_status == "delivered" and (tracking_number or proof_hash):
        return "strong"
    if delivery_status in ("shipped", "delivered") or tracking_number or proof_hash:
        return "weak"
    return "none"


def allowed_dispute_actions(
    *, amount_usd: float, strength: str, hours_left: float, settings: Settings
) -> tuple[set[str], str | None]:
    """Devuelve (acciones permitidas, código de error del catálogo si aplica escalamiento forzoso)."""
    if hours_left < settings.dispute_min_hours_left:
        return {HUMAN}, "E_DISPUTE_DEADLINE_NEAR"
    if amount_usd > settings.dispute_auto_max_usd:
        return {HUMAN}, "E_DISPUTE_HIGH_VALUE"
    if strength == "strong":
        return {CONTEST, HUMAN}, None
    if strength == "weak":
        return {CONTEST, ACCEPT, HUMAN}, None
    return {ACCEPT, HUMAN}, None


def rule_based_decision(allowed: set[str], strength: str) -> str:
    """Decisión de respaldo (sin LLM). Preferencia: contestar con prueba > aceptar > humano."""
    if strength in ("strong", "weak") and CONTEST in allowed:
        return CONTEST
    if ACCEPT in allowed:
        return ACCEPT
    return HUMAN
