from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    # --- PayPal (SIEMPRE sandbox en el hackathon) ---
    paypal_client_id: str = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_secret: str = os.getenv("PAYPAL_SECRET", "")
    paypal_sandbox: bool = True  # fijo: no hay modo live en este repo a propósito

    # --- LLM ---
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    # --- Infra ---
    db_path: str = os.getenv("PUENTE_DB", str(ROOT / "puente.db"))
    mock_mode: bool = os.getenv("PUENTE_MOCK", "1") == "1"
    fx_url: str = os.getenv("FX_URL", "")            # proveedor de tasa: verificar y fijar
    fx_json_path: str = os.getenv("FX_JSON_PATH", "rate")

    # --- Reglas de negocio / guardrails ---
    max_amount_usd: float = field(default_factory=lambda: _f("MAX_AMOUNT_USD", 200))
    min_kyc_level: int = 1
    quote_ttl_minutes: int = 10
    quote_grace_minutes: int = 5
    fx_max_age_minutes: int = 15
    fx_drift_tolerance_pct: float = 1.0       # deriva de tasa aceptable para honrar cotización vencida
    ves_amount_tolerance_pct: float = 0.5
    ves_confirm_timeout_min: int = 30
    provider_approval_timeout_min: int = 15
    delivery_deadline_hours: int = 24
    auto_complete_hours: int = 72
    max_provider_failovers: int = 2

    # Disputas: tope de lo que el agente puede decidir SOLO.
    dispute_auto_max_usd: float = field(default_factory=lambda: _f("DISPUTE_AUTO_MAX_USD", 50))
    dispute_min_hours_left: int = 48          # menos que esto -> humano
