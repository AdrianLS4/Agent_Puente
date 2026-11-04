"""PUENTE Concierge — Agente Conversacional Autónomo (ReAct con Tool Loop y Límites en Código).

Diseño y arquitectura:
1. Memoria: Historial conversacional completo preservando contexto.
2. Loop de Herramientas: El modelo decide si consultar catálogo, cotizar, verificar estado o registrar pagos.
3. Límites en Código:
   - El LLM solo propone; el código exige confirmación explícita del usuario (¿Confirmas? s/n).
   - Bancos desconocidos dan error inmediato, sin valores por defecto inventados.
   - Datos sensibles de Pago Móvil van a pantalla directa (outbox) y NUNCA al contexto del LLM.
   - Manejo a prueba de fallos de argumentos inválidos, bucles y herramientas desconocidas.
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Callable

from .cli import build
from .config import Settings
from .state_machine import State as S

MAX_TOOL_STEPS = 10

# Mapeo estricto de bancos venezolanos (nombre/alias -> código de 4 dígitos)
KNOWN_BANKS = {
    "0102": "0102",
    "venezuela": "0102",
    "bdv": "0102",
    "banco de venezuela": "0102",
    "0104": "0104",
    "venezolano de credito": "0104",
    "0105": "0105",
    "mercantil": "0105",
    "banco mercantil": "0105",
    "0108": "0108",
    "provincial": "0108",
    "bbva": "0108",
    "bbva provincial": "0108",
    "0114": "0114",
    "bancaribe": "0114",
    "0115": "0115",
    "exterior": "0115",
    "0134": "0134",
    "banesco": "0134",
    "0151": "0151",
    "fondo comun": "0151",
    "bfc": "0151",
    "0163": "0163",
    "tesoro": "0163",
    "banco del tesoro": "0163",
    "0166": "0166",
    "agricola": "0166",
    "0168": "0168",
    "bancrecer": "0168",
    "0169": "0169",
    "mibanco": "0169",
    "0171": "0171",
    "activo": "0171",
    "banco activo": "0171",
    "0172": "0172",
    "bancamiga": "0172",
    "0174": "0174",
    "banplus": "0174",
    "0175": "0175",
    "bicentenario": "0175",
    "0177": "0177",
    "banfanb": "0177",
    "0191": "0191",
    "bnc": "0191",
    "banco nacional de credito": "0191",
}


def resolve_bank(name: str) -> str | None:
    """Resuelve el banco venezolano al código de 4 dígitos. NUNCA adivina ni asume por defecto."""
    if not name:
        return None
    raw = str(name).strip().lower()
    if not raw:
        return None
    # Si ingresaron exactamente 4 dígitos
    if re.fullmatch(r"01\d{2}", raw):
        return raw if raw in KNOWN_BANKS else None
    return KNOWN_BANKS.get(raw)


SYSTEM_PROMPT = """Eres PUENTE, un agente de compras y pagos P2P para usuarios en Venezuela.
Hablas con un tono amable, tuteo venezolano natural y profesional ("chamo", "pana", "dale", "claro").

REGLAS ESTRICTAS DE SEGURIDAD Y OPERACIÓN:
1. NUNCA inventes productos ni confirmes compras que no existan en el catálogo. Si el usuario pide algo que no tienes (ej. Platzi), consulta el catálogo con `list_catalog` y dile con sinceridad qué opciones similares tienes.
2. NUNCA asumas el banco del usuario. Si no lo especificó, pregúntale explícitamente: "¿Con qué banco vas a pagar en Bolívares?".
3. Cuando tengas el producto y el banco, usa `get_quotes` para cotizar. Explica las opciones con datos humanos: reputación, operaciones completadas, margen y porcentaje de disputas.
4. Cuando el usuario elija una opción, usa `propose_purchase(rank=...)`. Aclara que no se cobrará nada hasta que confirme con (s/n).
5. No puedes procesar pagos recurrentes o suscripciones automáticas (solo compras de pago único).
6. Los datos de Pago Móvil del proveedor se mostrarán automáticamente en la pantalla del usuario una vez que el proveedor apruebe en PayPal. Tú nunca manejas números de cuenta bancaria ni cédulas sensibles.
"""

CONCIERGE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_catalog",
            "description": "Consulta la lista de productos y servicios digitales disponibles para compra en PUENTE.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_quotes",
            "description": "Obtiene cotizaciones en tiempo real para un item del catálogo y un banco venezolano.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer", "description": "ID del producto en el catálogo"},
                    "bank": {"type": "string", "description": "Nombre o código de 4 dígitos del banco emisor venezolano"},
                },
                "required": ["item_id", "bank"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_purchase",
            "description": "Prepara la propuesta formal de compra con el proveedor del rank indicado. Requiere confirmación posterior del usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rank": {"type": "integer", "description": "Número de ranking del proveedor (1, 2 o 3)"},
                },
                "required": ["rank"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_status",
            "description": "Verifica el estado actual de la orden y la retención de dólares en PayPal.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_payment",
            "description": "Registra el número de referencia del Pago Móvil realizado por el usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bank_reference": {"type": "string", "description": "Número de comprobante o referencia bancaria"},
                    "amount_ves": {"type": "number", "description": "Monto transferido en Bolívares (opcional)"},
                },
                "required": ["bank_reference"],
            },
        },
    },
]


class Concierge:
    def __init__(self, orch: Any, conn: Any, llm: Any, out: Callable[[str], None] | None = None, quiet: bool = False):
        self.orch = orch
        self.conn = conn
        self.llm = llm
        self.out = out or print
        self.quiet = quiet
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.pending: dict[str, Any] | None = None
        self.active_request_id: int | None = None
        self.last_offers: list[dict[str, Any]] = []
        self.outbox: list[str] = []

    def flush_outbox(self) -> list[str]:
        items = list(self.outbox)
        self.outbox.clear()
        return items

    def _trimmed(self) -> list[dict[str, Any]]:
        """Ventana de historial que nunca empieza en medio de una llamada a herramienta y preserva el system prompt."""
        if len(self.messages) <= 41:
            return list(self.messages)
        sys_msg = self.messages[0]
        tail = self.messages[1:]
        # Tomar los últimos 40
        chunk = tail[-40:]
        # Asegurarse de que el primer mensaje empiece con role=='user'
        idx = 0
        while idx < len(chunk) and chunk[idx].get("role") != "user":
            idx += 1
        trimmed_chunk = chunk[idx:] if idx < len(chunk) else chunk[-1:]
        return [sys_msg] + trimmed_chunk

    def _execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "list_catalog":
            rows = self.conn.execute(
                "SELECT id, title, price_usd, billing_kind FROM catalog_items WHERE active=1"
            ).fetchall()
            items = []
            for r in rows:
                b_kind = r["billing_kind"] if "billing_kind" in r.keys() else "one_off"
                if b_kind != "recurring" and "mensual" not in r["title"].lower():
                    items.append({"id": r["id"], "title": r["title"], "price_usd": r["price_usd"]})
            return {"items": items}

        if name == "get_quotes":
            raw_bank = args.get("bank", "")
            bank_code = resolve_bank(raw_bank)
            if not bank_code:
                return {"error": f"Banco '{raw_bank}' no reconocido o no soportado. Debe ser un banco venezolano válido."}

            item_id = args.get("item_id")
            if not isinstance(item_id, int):
                try:
                    item_id = int(item_id)
                except (ValueError, TypeError):
                    return {"error": f"item_id inválido: {item_id}"}

            item_row = self.conn.execute("SELECT * FROM catalog_items WHERE id=?", (item_id,)).fetchone()
            if not item_row:
                return {"error": f"Item con ID {item_id} no encontrado en el catálogo."}

            b_kind = item_row["billing_kind"] if "billing_kind" in item_row.keys() else "one_off"
            if b_kind == "recurring" or "mensual" in item_row["title"].lower():
                return {
                    "code": "E_RECURRING_UNSUPPORTED",
                    "error": "Cobros recurrentes no soportados. PUENTE opera exclusivamente compras de pago único.",
                }

            key = f"concierge-{int(time.time() * 1000)}"
            req_res = self.orch.create_request(1, item_id, bank_code, key)
            if not req_res.ok:
                return {"error": req_res.message_es}

            rid = req_res.data["request_id"]
            self.active_request_id = rid

            q_res = self.orch.quote(rid)
            if not q_res.ok:
                return {"error": q_res.message_es}

            offers = []
            for q in q_res.data.get("quotes", []):
                p = self.conn.execute(
                    "SELECT completed_orders, disputed_orders, avg_settle_minutes FROM providers WHERE id=?",
                    (q["provider_id"],)
                ).fetchone()
                offers.append({
                    "rank": q["rank"],
                    "display_name": q["display_name"],
                    "total_ves": round(q["total_ves"], 2),
                    "score": round(q["score"], 1),
                    "completed_ops": p["completed_orders"] if p else 0,
                    "disputes": p["disputed_orders"] if p else 0,
                    "avg_settle_minutes": p["avg_settle_minutes"] if p else 20,
                    "margin_pct": q.get("margin_pct", 0),
                })
            self.last_offers = offers
            return {"request_id": rid, "offers": offers}

        if name == "propose_purchase":
            rank = args.get("rank")
            if not isinstance(rank, int):
                try:
                    rank = int(rank)
                except (ValueError, TypeError):
                    return {"error": f"Rank inválido: {rank}"}

            matched = next((o for o in self.last_offers if o["rank"] == rank), None)
            if not matched:
                return {"error": f"Opción #{rank} no disponible en las cotizaciones vigentes."}

            self.pending = {
                "request_id": self.active_request_id,
                "rank": rank,
                "total_ves": matched["total_ves"],
                "offer": matched,
            }
            return {
                "status": "PENDING_CONFIRMATION",
                "message": f"Propuesta armada para opción #{rank} ({matched['display_name']}) por {matched['total_ves']:.2f} Bs. Esperando confirmación del usuario.",
            }

        if name == "check_status":
            rid = self.active_request_id
            if not rid:
                # Buscar la última orden abierta
                r_row = self.conn.execute("SELECT id, state FROM purchase_requests ORDER BY id DESC LIMIT 1").fetchone()
                if r_row:
                    rid = r_row["id"]
                    self.active_request_id = rid

            if not rid:
                return {"state": "NONE", "message": "No hay operaciones activas."}

            req = self.conn.execute("SELECT state FROM purchase_requests WHERE id=?", (rid,)).fetchone()
            curr_state = req["state"] if req else "UNKNOWN"

            # Si está esperando aprobación del proveedor, verificar si PayPal ya fue aprobado
            if curr_state in (S.AWAITING_PROVIDER_APPROVAL, S.PAYPAL_ORDER_CREATED):
                o = self.conn.execute("SELECT * FROM paypal_orders WHERE request_id=? ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
                if o:
                    try:
                        p_order = self.orch.gw.get_order(o["paypal_order_id"])
                        if p_order.get("status") == "APPROVED":
                            self.orch._to(rid, S.AWAITING_VES)
                            curr_state = S.AWAITING_VES
                    except Exception:
                        pass

            if curr_state == S.AWAITING_VES or curr_state == S.USD_ESCROW_LOCKED:
                ticket = self.orch.get_pago_movil_ticket(rid)
                phone_digits = re.sub(r"\D", "", ticket["phone"])
                ticket_box = (
                    f"\n┌──────────────────────────────────────────────────────────────┐\n"
                    f"│ 🔒 TICKET DE PAGO MÓVIL (Dólares retenidos en PayPal Escrow) │\n"
                    f"├──────────────────────────────────────────────────────────────┤\n"
                    f"│ Proveedor:    {ticket['provider_name']:<47}│\n"
                    f"│ Banco:        {ticket['bank_name']} ({ticket['bank_code']})\n"
                    f"│ Teléfono:     {ticket['phone']} ({phone_digits})\n"
                    f"│ C.I./RIF:     {ticket['id_doc']:<47}│\n"
                    f"│ Titular:      {ticket['holder_name']:<47}│\n"
                    f"│ Monto exacto: {ticket['total_ves']:>10.2f} Bs.{' '*34}│\n"
                    f"│ Concepto:     {ticket['concept_code']:<47}│\n"
                    f"└──────────────────────────────────────────────────────────────┘\n"
                )
                self.outbox.append(ticket_box)
                if self.out:
                    self.out(ticket_box)
                return {
                    "state": "AWAITING_VES",
                    "message": "El proveedor ya aprobó la retención de dólares en PayPal. Se mostró el ticket con los datos de Pago Móvil en la pantalla del usuario. Pídele al usuario que realice el pago y proporcione el número de comprobante.",
                }

            return {"state": curr_state, "message": f"Estado de la operación: {curr_state}"}

        if name == "report_payment":
            rid = self.active_request_id
            if not rid:
                return {"error": "No hay solicitud activa para reportar pago."}
            ref = args.get("bank_reference")
            if not ref:
                return {"error": "Falta la referencia bancaria."}

            amount = args.get("amount_ves")
            if not amount:
                q_row = self.conn.execute(
                    "SELECT total_ves FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)",
                    (rid,)
                ).fetchone()
                amount = q_row["total_ves"] if q_row else 0.0

            res = self.orch.confirm_ves(rid, ref, amount)
            if not res.ok:
                return {"error": res.message_es}
            return {
                "state": "VES_REPORTED",
                "message": f"Pago Móvil registrado exitosamente con referencia {ref} por {amount:.2f} Bs. En espera de verificación del proveedor.",
            }

        return {"error": f"Herramienta desconocida: {name}"}

    def turn(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})

        steps = 0
        while steps < MAX_TOOL_STEPS:
            steps += 1
            trimmed_msgs = self._trimmed()
            resp = self.llm.complete(trimmed_msgs, CONCIERGE_TOOLS)
            self.messages.append(resp)

            tool_calls = resp.get("tool_calls")
            if not tool_calls:
                return resp.get("content") or ""

            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                tc_id = tc.get("id", f"call_{steps}")

                if not self.quiet and self.out:
                    self.out(f"⚙️ [Agente pensando] Ejecutando: {name}({raw_args})")

                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        result = {"error": f"Argumentos inválidos: se esperaba objeto JSON, se recibió {type(args).__name__}"}
                    else:
                        result = self._execute_tool(name, args)
                except Exception as exc:
                    result = {"error": f"Error al ejecutar {name}: {str(exc)}"}

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result),
                })

        return "Disculpa, me enredé con tantas herramientas y procesos. ¿Podrías indicarme qué necesitás de nuevo?"

    def confirm_pending(self) -> Any:
        if not self.pending:
            return None
        rid = self.pending["request_id"]
        rank = self.pending["rank"]
        self.orch.select_quote(rid, rank)
        res = self.orch.create_paypal_order(rid)
        self.pending = None
        self.messages.append({"role": "user", "content": f"[SISTEMA] El usuario CONFIRMÓ la propuesta de compra para la opción #{rank}."})
        return res

    def cancel_pending(self) -> None:
        self.pending = None
        self.messages.append({"role": "user", "content": "[SISTEMA] El usuario CANCELÓ la propuesta de compra."})


class OpenAIToolLLM:
    """Cliente para LLMs con soporte de Function Calling (OpenAI o Gemini vía OpenAI-compatible endpoint)."""

    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model
        self.calls = 0

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        self.calls += 1
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        msg = resp.choices[0].message
        if hasattr(msg, "model_dump"):
            dump = msg.model_dump(exclude_unset=True)
        else:
            dump = dict(msg)
        dump["role"] = "assistant"
        return dump


def build_live_llm(settings: Settings) -> Any:
    """Inicializa el cliente de LLM en vivo con Gemini (OpenAI compat) o OpenAI directo."""
    gemini_key = settings.gemini_api_key or (settings.openai_api_key if settings.openai_api_key.startswith("AIza") else "")
    if gemini_key:
        from openai import OpenAI
        client = OpenAI(
            api_key=gemini_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        model = settings.gemini_model or "gemini-2.5-flash"
        return OpenAIToolLLM(client, model)
    elif settings.openai_api_key:
        from openai import OpenAI
        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
        model = settings.openai_model or "gpt-4.1-mini"
        return OpenAIToolLLM(client, model)
    raise RuntimeError("No hay clave de LLM configurada (GEMINI_API_KEY o OPENAI_API_KEY en .env)")


def run_repl(concierge: Concierge, gw: Any, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print) -> None:
    """Loop conversacional interactivo (REPL) con confirmaciones en código y comandos de proveedor."""
    out("━" * 65)
    out("🤖 PUENTE Concierge — Asistente Autónomo P2P con IA")
    out("   • Escribe libremente lo que buscas comprar y cómo deseas pagar.")
    out("   • Comandos de prueba: /carlos aprobar  |  /carlos confirmar  |  salir")
    out("━" * 65)

    while True:
        try:
            user_in = input_fn("\n💬 Vos: ").strip()
        except (EOFError, StopIteration):
            break

        if not user_in:
            continue
        if user_in.lower() in ("salir", "exit", "quit"):
            out("👋 ¡Hasta luego!")
            break

        if user_in.startswith("/carlos"):
            parts = user_in.split()
            cmd = parts[1] if len(parts) > 1 else ""
            if cmd == "aprobar":
                row = concierge.conn.execute("SELECT paypal_order_id FROM paypal_orders ORDER BY id DESC LIMIT 1").fetchone()
                if row:
                    gw.approve(row[0])
                    out(f"✅ [Carlos] Orden PayPal '{row[0]}' aprobada en Sandbox.")
                else:
                    out("❌ No hay órdenes en PayPal para aprobar.")
            elif cmd == "confirmar":
                row = concierge.conn.execute("SELECT request_id FROM paypal_orders ORDER BY id DESC LIMIT 1").fetchone()
                if row:
                    concierge.orch.provider_release_and_capture(row[0])
                    out(f"✅ [Carlos] Proveedor confirmó recepción de Bolívares. Dólares liberados.")
                else:
                    out("❌ No hay solicitud activa.")
            continue

        reply = concierge.turn(user_in)
        out(f"\n🤖 PUENTE: {reply}")

        # Mensajes de pantalla directa (ticket de pago móvil)
        for msg in concierge.flush_outbox():
            out(msg)

        # Si el agente propuso una compra, Python pide confirmación explícita
        if concierge.pending:
            offer = concierge.pending.get("offer", {})
            total_ves = concierge.pending.get("total_ves", 0.0)
            conf = input_fn(f"\n❓ ¿Confirmas la compra por {total_ves:.2f} Bs.? (s/n): ").strip().lower()
            if conf in ("s", "si", "sí", "y", "yes"):
                res = concierge.confirm_pending()
                if res.ok:
                    out("✅ Compra confirmada por el usuario. Orden creada en PayPal.")
                    follow_up = concierge.turn("[SISTEMA] El usuario CONFIRMÓ la compra. Procede con el siguiente paso.")
                    out(f"\n🤖 PUENTE: {follow_up}")
                    for msg in concierge.flush_outbox():
                        out(msg)
            else:
                concierge.cancel_pending()
                out("Cancelado. No se movió nada.")


def main() -> int:
    settings = Settings()
    live = "--live" in sys.argv
    quiet = "--quiet" in sys.argv

    conn, orch, gw, _ = build(mock=not live)
    if live:
        llm = build_live_llm(settings)
    else:
        # Fallback para pruebas sin conexión
        try:
            llm = build_live_llm(settings)
        except Exception:
            from .agents import AgentBrain
            llm = AgentBrain(settings)

    concierge = Concierge(orch, conn, llm, quiet=quiet)
    run_repl(concierge, gw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
