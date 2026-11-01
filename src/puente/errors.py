"""Catálogo de errores PRE-ESCRITOS.

Cada error conocido del flujo tiene: etapa, severidad, acción automática, mensaje al cliente (ES)
y un playbook. El agente NO improvisa ante errores de dinero: el código consulta este catálogo.

Imprimir como tabla markdown:  python -m puente.errors > docs/ERRORS.md
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    RETRY = "retry"                          # reintentar con backoff exponencial
    WAIT = "wait"                            # no hacer nada; esperar evento/webhook/timeout
    FAILOVER_PROVIDER = "failover_provider"  # devolver VES, penalizar proveedor, reabrir con el siguiente
    REQUOTE = "requote"                      # nueva cotización
    COMPENSATE = "compensate"                # reembolsar (PayPal si aplica) y devolver VES
    HUMAN_REVIEW = "human_review"            # cola humana; el agente se detiene
    ABORT = "abort"                          # cerrar sin dinero movido
    TREAT_AS_SUCCESS = "treat_as_success"    # idempotencia: ya estaba hecho
    CONTEST_DISPUTE = "contest_dispute"
    ACCEPT_DISPUTE = "accept_dispute"


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    stage: str
    severity: Severity
    action: Action
    user_msg_es: str
    playbook: str
    retryable: bool = False
    max_retries: int = 0
    backoff_s: float = 0.0
    on_exhausted: Action = Action.HUMAN_REVIEW
    next_state: str | None = None


@dataclass(frozen=True)
class Resolution:
    code: str
    action: Action
    delay_s: float
    next_state: str | None
    user_msg_es: str
    spec: ErrorSpec

    @property
    def needs_human(self) -> bool:
        return self.action == Action.HUMAN_REVIEW


CATALOG: dict[str, ErrorSpec] = {}


def _e(**kw) -> None:
    spec = ErrorSpec(**kw)
    assert spec.code not in CATALOG, spec.code
    CATALOG[spec.code] = spec


# ───────────── SOURCING / COTIZACIÓN ─────────────
_e(code="E_AMOUNT_LIMIT", stage="intake", severity=Severity.WARN, action=Action.ABORT,
   user_msg_es="El monto supera el límite por operación. Divide la compra o contacta soporte.",
   playbook="Rechazar antes de cotizar. No se mueve dinero.")
_e(code="E_NO_PROVIDERS", stage="sourcing", severity=Severity.WARN, action=Action.ABORT,
   next_state="FAILED",
   user_msg_es="Ahora mismo no hay proveedores disponibles para este monto. Intenta en unos minutos.",
   playbook="Ningún proveedor pasó los filtros duros. Registrar motivos de rechazo por proveedor; no cobrar nada.")
_e(code="E_FX_UNAVAILABLE", stage="sourcing", severity=Severity.ERROR, action=Action.ABORT,
   next_state="FAILED",
   user_msg_es="No pudimos obtener la tasa de cambio. Intenta de nuevo en unos minutos.",
   playbook="Falló la fuente de tasa. Se usa el último valor guardado SOLO si tiene menos de fx_max_age; "
            "si no hay ninguno vigente, no cotizar (nunca inventar una tasa).")
_e(code="E_QUOTE_EXPIRED", stage="sourcing", severity=Severity.WARN, action=Action.REQUOTE,
   user_msg_es="Tu cotización venció. Te mostramos una nueva; si ya pagaste, te devolvemos o ajustamos.",
   playbook="Si el cliente ya pagó VES y la deriva de tasa ≤ tolerancia → honrar. Si no → COMPENSATE.")

# ───────────── PAGO EN VES ─────────────
_e(code="E_VES_TIMEOUT", stage="ves", severity=Severity.INFO, action=Action.ABORT, next_state="EXPIRED",
   user_msg_es="No recibimos tu pago en el tiempo previsto, la solicitud se cerró sin cargos.",
   playbook="AWAITING_VES venció. Nada que compensar: no se movió dinero.")
_e(code="E_VES_AMOUNT_MISMATCH", stage="ves", severity=Severity.ERROR, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="El monto recibido no coincide con la cotización. Un operador lo revisará.",
   playbook="Pago parcial o excedido. No avanzar a PayPal. Humano decide devolver diferencia o completar.")
_e(code="E_VES_DUPLICATE_REF", stage="ves", severity=Severity.CRITICAL, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="Esa referencia de pago ya fue usada. Un operador revisará tu caso.",
   playbook="Referencia bancaria reutilizada = posible fraude. Bloquear y escalar.")

# ───────────── PAYPAL ─────────────
_e(code="E_PP_AUTH", stage="paypal", severity=Severity.ERROR, action=Action.RETRY,
   retryable=True, max_retries=1, backoff_s=0, on_exhausted=Action.HUMAN_REVIEW,
   user_msg_es="Estamos reconectando con PayPal, un momento…",
   playbook="401: token vencido. El cliente pide token nuevo en cada llamada; reintentar 1 vez. Si persiste: credenciales mal → humano.")
_e(code="E_PP_RATE_LIMIT", stage="paypal", severity=Severity.WARN, action=Action.RETRY,
   retryable=True, max_retries=4, backoff_s=2, on_exhausted=Action.HUMAN_REVIEW,
   user_msg_es="PayPal está ocupado, reintentando…",
   playbook="429: backoff exponencial (2,4,8,16 s).")
_e(code="E_PP_NETWORK", stage="paypal", severity=Severity.WARN, action=Action.RETRY,
   retryable=True, max_retries=3, backoff_s=2, on_exhausted=Action.HUMAN_REVIEW,
   user_msg_es="Problema de conexión con PayPal, reintentando…",
   playbook="Timeout/5xx. ANTES de reintentar una creación, consultar la BD/PayPal para no duplicar la orden.")
_e(code="E_PP_UNPROCESSABLE", stage="paypal", severity=Severity.ERROR, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="PayPal rechazó la operación. Un operador la revisará.",
   playbook="422 sin código conocido. Guardar el cuerpo completo en error_events y escalar.")
_e(code="E_PP_NOT_FOUND", stage="paypal", severity=Severity.ERROR, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="No encontramos la orden en PayPal. Un operador lo revisará.",
   playbook="404 al consultar/capturar: IDs desincronizados. No reintentar a ciegas.")
_e(code="E_PP_INSTRUMENT_DECLINED", stage="paypal", severity=Severity.WARN, action=Action.WAIT,
   user_msg_es="(Proveedor) PayPal rechazó la fuente de fondos. Elige otra tarjeta/saldo y vuelve a aprobar.",
   playbook="INSTRUMENT_DECLINED: la orden sigue viva; el proveedor puede reaprobar con otro instrumento hasta el timeout.")
_e(code="E_PP_NOT_APPROVED", stage="paypal", severity=Severity.WARN, action=Action.FAILOVER_PROVIDER,
   next_state="REFUND_VES_PENDING",
   user_msg_es="El proveedor no completó el pago a tiempo. Te devolvemos tus Bs. y te ofrecemos otro proveedor.",
   playbook="No aprobó en provider_approval_timeout. Devolver VES, failed_orders+1 al proveedor, reabrir excluyéndolo (nuevo pago VES).")
_e(code="E_PP_ALREADY_CAPTURED", stage="paypal", severity=Severity.INFO, action=Action.TREAT_AS_SUCCESS,
   user_msg_es="Tu pago ya estaba confirmado.",
   playbook="ORDER_ALREADY_CAPTURED: idempotente. Leer la orden y continuar como éxito.")
_e(code="E_PP_CAPTURE_PENDING", stage="paypal", severity=Severity.WARN, action=Action.WAIT,
   user_msg_es="PayPal está revisando el pago. Te avisamos apenas se confirme.",
   playbook="Captura en PENDING (revisión). NO entregar. Esperar webhook PAYMENT.CAPTURE.COMPLETED/DENIED.")
_e(code="E_PP_AMOUNT_MISMATCH", stage="paypal", severity=Severity.CRITICAL, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="Detectamos una diferencia en el monto pagado. Un operador lo revisará.",
   playbook="Monto capturado ≠ monto esperado. Congelar y escalar.")
_e(code="E_PP_DUPLICATE_INVOICE", stage="paypal", severity=Severity.INFO, action=Action.TREAT_AS_SUCCESS,
   user_msg_es="La factura ya existía.",
   playbook="Número de factura repetido: recuperar la existente por invoice_number en vez de crear otra.")

# ───────────── ENTREGA / TRACKING ─────────────
_e(code="E_TRACKING_INVALID", stage="delivery", severity=Severity.WARN, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="No pudimos registrar el seguimiento del envío. Un operador lo revisará.",
   playbook="PayPal rechazó el tracker (carrier/número). Reintentar con carrier OTHER una vez; si falla, manual.")
_e(code="E_DELIVERY_TIMEOUT", stage="delivery", severity=Severity.ERROR, action=Action.COMPENSATE,
   next_state="REFUND_VES_PENDING",
   user_msg_es="El comercio no entregó a tiempo. Te devolvemos tus Bs.",
   playbook="Sin prueba de entrega al vencer delivery_deadline: reembolso PayPal (captura) + devolución VES.")
_e(code="E_VES_REFUND_FAILED", stage="compensation", severity=Severity.CRITICAL, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="Estamos procesando tu devolución manualmente. Te contactaremos.",
   playbook="La devolución en VES falló. Prioridad máxima en la cola humana.")

# ───────────── DISPUTAS ─────────────
_e(code="E_DISPUTE_HIGH_VALUE", stage="dispute", severity=Severity.WARN, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="Tu caso fue escalado a un especialista por su monto.",
   playbook="amount > dispute_auto_max_usd: el agente prepara el expediente, un humano decide.")
_e(code="E_DISPUTE_DEADLINE_NEAR", stage="dispute", severity=Severity.ERROR, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="Tu caso fue escalado con prioridad.",
   playbook="Menos de dispute_min_hours_left para responder: humano con prioridad alta.")
_e(code="E_DISPUTE_NO_EVIDENCE", stage="dispute", severity=Severity.INFO, action=Action.ACCEPT_DISPUTE,
   user_msg_es="Aceptamos el reclamo y te reembolsamos.",
   playbook="Sin prueba de entrega y monto bajo: aceptar es más barato que perder.")
_e(code="E_DISPUTE_STRONG_EVIDENCE", stage="dispute", severity=Severity.INFO, action=Action.CONTEST_DISPUTE,
   user_msg_es="Presentamos tu comprobante de entrega a PayPal.",
   playbook="Prueba fuerte (tracking entregado / comprobante digital con hash y fecha): contestar con evidencia.")
_e(code="E_AGENT_ESCALATED", stage="dispute", severity=Severity.INFO, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="Tu caso fue escalado a un especialista para revisarlo con cuidado.",
   playbook="El propio agente eligió 'human' dentro de las acciones permitidas (caso dudoso).")
_e(code="E_GUARDRAIL_BLOCKED", stage="agent", severity=Severity.WARN, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="Tu caso fue escalado a un operador.",
   playbook="El LLM propuso una acción fuera de las permitidas por la política. Se ignora y se escala.")

# ───────────── SISTEMA ─────────────
_e(code="E_IDEMPOTENT_REPLAY", stage="system", severity=Severity.INFO, action=Action.TREAT_AS_SUCCESS,
   user_msg_es="Esa solicitud ya estaba registrada.",
   playbook="Misma idempotency_key: devolver la solicitud existente; no crear otra.")
_e(code="E_INVALID_TRANSITION", stage="system", severity=Severity.ERROR, action=Action.HUMAN_REVIEW,
   user_msg_es="Estado inconsistente. Un operador lo revisará.",
   playbook="Transición no permitida por la máquina de estados: bug o evento fuera de orden.")
_e(code="E_UNKNOWN", stage="system", severity=Severity.ERROR, action=Action.HUMAN_REVIEW,
   next_state="HUMAN_REVIEW",
   user_msg_es="Ocurrió un error inesperado. Un operador lo revisará.",
   playbook="Cualquier cosa no mapeada. Guardar traza completa.")


def resolve(code: str, attempt: int = 0) -> Resolution:
    """Decide qué hacer ante `code` en el intento número `attempt` (0 = primer fallo)."""
    spec = CATALOG.get(code) or CATALOG["E_UNKNOWN"]
    if spec.retryable:
        if attempt < spec.max_retries:
            return Resolution(spec.code, Action.RETRY, spec.backoff_s * (2 ** attempt), None,
                              spec.user_msg_es, spec)
        return Resolution(spec.code, spec.on_exhausted, 0.0, spec.next_state, spec.user_msg_es, spec)
    return Resolution(spec.code, spec.action, 0.0, spec.next_state, spec.user_msg_es, spec)


def map_paypal_error(status: int | None, name: str = "", issue: str = "") -> str:
    """Traduce un error HTTP de PayPal a un código del catálogo."""
    issue = (issue or "").upper()
    if issue == "ORDER_ALREADY_CAPTURED":
        return "E_PP_ALREADY_CAPTURED"
    if issue == "INSTRUMENT_DECLINED":
        return "E_PP_INSTRUMENT_DECLINED"
    if issue in ("ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"):
        return "E_PP_NOT_APPROVED"
    if issue == "DUPLICATE_INVOICE_NUMBER":
        return "E_PP_DUPLICATE_INVOICE"
    if status == 401:
        return "E_PP_AUTH"
    if status == 404:
        return "E_PP_NOT_FOUND"
    if status == 429:
        return "E_PP_RATE_LIMIT"
    if status is None or status == 0 or status >= 500:
        return "E_PP_NETWORK"
    if status == 422:
        return "E_PP_UNPROCESSABLE"
    return "E_UNKNOWN"


def catalog_markdown() -> str:
    rows = ["| Código | Etapa | Sev. | Acción | Reintentos | Mensaje al usuario (ES) | Playbook |",
            "|---|---|---|---|---|---|---|"]
    for s in CATALOG.values():
        retries = f"{s.max_retries}× (backoff {s.backoff_s:g}s)" if s.retryable else "—"
        rows.append(f"| `{s.code}` | {s.stage} | {s.severity.value} | `{s.action.value}` | {retries} "
                    f"| {s.user_msg_es} | {s.playbook} |")
    return "\n".join(rows)


if __name__ == "__main__":
    print("# Catálogo de errores\n")
    print(catalog_markdown())
