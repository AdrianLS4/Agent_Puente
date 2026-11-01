"""Tasa de referencia USD/VES.

NUNCA se inventa una tasa: si no hay tasa fresca, o último valor de respaldo dentro de la edad
máxima, se aborta con E_FX_UNAVAILABLE.

`fetch` es inyectable: en mock devuelve un valor fijo; en real llama a FX_URL (elige y VERIFICA tú
la fuente — p. ej. el dato oficial del BCV o un agregador — antes del demo).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

import httpx

from .db import iso, parse_iso


@dataclass
class FxRate:
    rate: float
    source: str
    fetched_at: datetime
    is_fallback: bool = False


class FxError(Exception):
    code = "E_FX_UNAVAILABLE"


Fetcher = Callable[[], tuple[float, str]]   # -> (rate, source)


def mock_fetcher(rate: float = 40.0) -> Fetcher:
    return lambda: (rate, "mock")


def http_fetcher(url: str, json_path: str = "rate", timeout: float = 5.0) -> Fetcher:
    def _f() -> tuple[float, str]:
        data = httpx.get(url, timeout=timeout).json()
        for part in json_path.split("."):
            data = data[part]
        return float(data), url
    return _f


def get_reference_rate(
    conn: sqlite3.Connection, fetch: Fetcher, now: datetime, max_age_minutes: int
) -> FxRate:
    try:
        rate, source = fetch()
        if rate <= 0:
            raise ValueError("rate <= 0")
        conn.execute("INSERT INTO fx_rates (source, rate, fetched_at, is_fallback) VALUES (?,?,?,0)",
                     (source, rate, iso(now)))
        conn.commit()
        return FxRate(rate, source, now, False)
    except Exception:
        row = conn.execute(
            "SELECT rate, source, fetched_at FROM fx_rates ORDER BY fetched_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if row and parse_iso(row["fetched_at"]) >= now - timedelta(minutes=max_age_minutes):
            return FxRate(row["rate"], row["source"], parse_iso(row["fetched_at"]), True)
        raise FxError("sin tasa fresca ni respaldo vigente")
