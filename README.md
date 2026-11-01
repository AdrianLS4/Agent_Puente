# PUENTE — compras en USD para venezolanos, con agentes y PayPal

Agente que recibe lo que un cliente quiere pagar en USD, **busca y rankea proveedores verificados** (con cotización en VES), **crea la orden real de PayPal** (sandbox) hacia el comercio, **hace seguimiento de pago y entrega** y **gestiona disputas** y fallos con un catálogo de errores pre-escrito.

Hackathon: *PayPal AI Hackathon — Build What's Next with PayPal and AI* (Devpost).

## Arranque rápido (sin credenciales: PayPal simulado)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q                 # 43 pruebas
python -m puente.cli demo           # 7 escenarios: feliz, reintentos, failover, entrega fallida, 3 disputas
python -m puente.cli demo failover  # uno solo
python -m puente.cli errors         # catálogo de errores en markdown
```

## Con PayPal sandbox real (día 1)

1. Copia `.env.example` a `.env` y completa `PAYPAL_CLIENT_ID` / `PAYPAL_SECRET` (ver `docs/FLOW.md` §1).
2. `python -m puente.cli live-check` → crea una orden real de $1 vía el **PayPal Agent Toolkit** y te da el `approve_url`.
3. Aprueba con tu cuenta sandbox *Personal* y valida los endpoints que **no** cubre el toolkit (reembolso, tracking, disputas) con Postman antes de confiar en ellos.
4. Con `OPENAI_API_KEY`: `python -m puente.cli demo dispute-contest --llm` usa el `ResolutionAgent` de verdad.

## Estructura

```
schema.sql                 BD completa (16 tablas)
docs/FLOW.md               flujo, algoritmo, máquina de estados, cuentas sandbox, limitaciones
docs/ERRORS.md             catálogo de 30 errores (generado desde el código)
src/puente/
  orchestrator.py          pasos del flujo + compensación + failover + disputas
  state_machine.py         estados y transiciones válidas (único sitio que cambia estado)
  ranking.py               filtros duros + score explicable (determinista)
  errors.py                catálogo, backoff, mapeo de errores de PayPal
  policy.py                guardrails de disputas (lo que el LLM puede elegir)
  paypal_gateway.py        LivePayPal (toolkit + REST) · MockPayPal (con inyección de fallos)
  agents.py                Intake / Sourcing / Resolution (Agents SDK) + PayPalOps (tools del toolkit)
  fx.py · db.py · seed.py · config.py · cli.py
tests/                     ranking, flujo completo, errores, disputas, guardrails
```

## Qué está verificado y qué no

**Verificado (corre aquí):** ranking, máquina de estados, orquestador de punta a punta con `MockPayPal`, reintentos/backoff, idempotencia, failover, compensación, política de disputas y guardrail contra acciones del LLM; el payload de `create_order` valida contra el toolkit real instalado; los agentes y `PayPalToolkit` se construyen.

**NO verificado:** ninguna llamada real a PayPal ni al LLM (no hay credenciales en este entorno). En concreto, los endpoints REST de **reembolso, tracking y disputas** (`paypal_gateway.py`, clase `PayPalRest`) están escritos según la documentación pública y pueden necesitar ajustes. La fuente de tasa USD/VES (`FX_URL`) queda por elegir.

**Ojo con el toolkit:** `paypal-agent-toolkit` 1.1.0 (el que instala pip) trae tools de órdenes, productos, suscripciones y facturas, pero **no** de disputas ni tracking, y sus imports son `paypal_agent_toolkit.shared.configuration` (la doc web muestra `common`). Revisa si hay versión nueva antes de escribir más REST propio.

`E_PP_DUPLICATE_INVOICE` y `E_INVALID_TRANSITION` están en el catálogo pero aún sin cablear (el camino de facturas no está implementado en el orquestador).

## Plan hasta el cierre (12 nov; entrega el 11)

| Día | Objetivo |
|---|---|
| 1 (1 nov) | Cuentas sandbox, `live-check`, validar refund/tracking/disputas con Postman |
| 2–3 | Modo real en el CLI (`PUENTE_MOCK=0`), fuente de tasa, probar el flujo completo en sandbox |
| 4–5 | Receptor de webhooks (FastAPI) + loop de `check_timeouts()` cada minuto |
| 6–7 | Probar `AgentBrain` con LLM real; UI mínima de chat (para el premio Best Demo Delivery) |
| 8–9 | Camino de facturas (`create_invoice`/`send_invoice`) como segunda vía de pago |
| 10 | Video de demo (3 min), README en inglés, historia de impacto con datos |
| 11 | Buffer y envío en Devpost (el repo debe ser open source) |
