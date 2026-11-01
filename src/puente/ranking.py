"""Búsqueda y ranking de proveedores. 100% determinista y explicable (sin LLM).

Fase 1 — FILTROS DUROS (descartan con motivo):
    inactivo · kyc bajo · PayPal no verificado · sin capacidad USD · banco no soportado · disputas altas
Fase 2 — SCORE 0-100 con pesos fijos:
    precio 40 · confiabilidad 25 · velocidad 15 · bajo % disputas 10 · KYC 5 · holgura de capacidad 5
Desempate: mayor confiabilidad, luego menor total en VES.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

WEIGHTS = {"price": 0.40, "reliability": 0.25, "speed": 0.15, "disputes": 0.10, "kyc": 0.05, "capacity": 0.05}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

PRIOR_A, PRIOR_B = 4.5, 0.5          # prior Beta: proveedor nuevo ≈ 90% confiable con peso de 5 operaciones
DISPUTE_RATE_CAP = 0.10              # 10% de disputas = puntaje 0 en ese criterio
DISPUTE_RATE_BAN = 0.10              # con >= MIN_ORDERS_FOR_BAN órdenes y >10% -> se descarta
MIN_ORDERS_FOR_BAN = 10
SPEED_ZERO_MINUTES = 60.0
PRICE_SPREAD = 0.10                  # un proveedor 10% más caro que el más barato obtiene 0 en precio


@dataclass
class ProviderProfile:
    id: int
    display_name: str
    kyc_level: int
    status: str
    paypal_verified: bool
    completed_orders: int
    disputed_orders: int
    failed_orders: int
    avg_settle_minutes: float
    usd_capacity_available: float
    margin_pct: float
    fee_usd: float
    accepts_banks: set[str] = field(default_factory=set)


@dataclass
class RankedQuote:
    provider_id: int
    display_name: str
    ref_rate: float
    rate_ves_per_usd: float
    margin_pct: float
    fee_usd: float
    total_ves: float
    score: float
    breakdown: dict[str, float]
    rank: int = 0


@dataclass
class RankingResult:
    quotes: list[RankedQuote]
    rejected: dict[int, str]          # provider_id -> motivo


def reliability(p: ProviderProfile) -> float:
    """Tasa de éxito suavizada (Beta). Evita que 1/1 operación valga más que 95/100."""
    total = p.completed_orders + p.disputed_orders + p.failed_orders
    return (p.completed_orders + PRIOR_A) / (total + PRIOR_A + PRIOR_B)


def dispute_rate(p: ProviderProfile) -> float:
    total = p.completed_orders + p.disputed_orders + p.failed_orders
    return (p.disputed_orders + 0.5) / (total + 5)


def speed_score(minutes: float) -> float:
    return max(0.0, min(1.0, 1.0 - minutes / SPEED_ZERO_MINUTES))


def hard_filter(p: ProviderProfile, amount_usd: float, bank: str | None, min_kyc: int) -> str | None:
    if p.status != "active":
        return "inactive"
    if not p.paypal_verified:
        return "paypal_unverified"
    if p.kyc_level < min_kyc:
        return "kyc_too_low"
    if p.usd_capacity_available < amount_usd:
        return "no_capacity"
    if bank and p.accepts_banks and bank not in p.accepts_banks:
        return "bank_unsupported"
    total = p.completed_orders + p.disputed_orders + p.failed_orders
    if total >= MIN_ORDERS_FOR_BAN and dispute_rate(p) > DISPUTE_RATE_BAN:
        return "high_dispute_rate"
    return None


def rank_providers(
    providers: list[ProviderProfile],
    *,
    amount_usd: float,
    bank: str | None,
    ref_rate: float,
    min_kyc: int = 1,
    exclude: set[int] | None = None,
    top_n: int = 3,
) -> RankingResult:
    exclude = exclude or set()
    rejected: dict[int, str] = {}
    candidates: list[tuple[ProviderProfile, float, float]] = []

    for p in providers:
        if p.id in exclude:
            rejected[p.id] = "excluded_previous_failure"
            continue
        reason = hard_filter(p, amount_usd, bank, min_kyc)
        if reason:
            rejected[p.id] = reason
            continue
        rate = ref_rate * (1 + p.margin_pct / 100)
        total = (amount_usd + p.fee_usd) * rate
        candidates.append((p, rate, total))

    if not candidates:
        return RankingResult([], rejected)

    best_total = min(t for _, _, t in candidates)
    quotes: list[RankedQuote] = []
    for p, rate, total in candidates:
        parts = {
            # 0% más caro que el mejor = 1.0; PRICE_SPREAD (10%) más caro o más = 0.0
            "price": max(0.0, 1.0 - (total / best_total - 1.0) / PRICE_SPREAD),
            "reliability": reliability(p),
            "speed": speed_score(p.avg_settle_minutes),
            "disputes": 1.0 - min(dispute_rate(p) / DISPUTE_RATE_CAP, 1.0),
            "kyc": p.kyc_level / 3,
            "capacity": min(p.usd_capacity_available / (amount_usd * 3), 1.0),
        }
        score = 100 * sum(WEIGHTS[k] * v for k, v in parts.items())
        quotes.append(RankedQuote(
            provider_id=p.id, display_name=p.display_name, ref_rate=ref_rate,
            rate_ves_per_usd=round(rate, 4), margin_pct=p.margin_pct, fee_usd=p.fee_usd,
            total_ves=round(total, 2), score=round(score, 2),
            breakdown={k: round(v, 4) for k, v in parts.items()},
        ))

    rel_by_id = {p.id: reliability(p) for p, _, _ in candidates}
    quotes.sort(key=lambda q: (-q.score, -rel_by_id[q.provider_id], q.total_ves))
    quotes = quotes[:top_n]
    for i, q in enumerate(quotes, start=1):
        q.rank = i
    return RankingResult(quotes, rejected)
