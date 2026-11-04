"""Concierge: el agente conversacional de PUENTE.

Diferencia con un "formulario con IA":
  - MEMORIA: el historial completo viaja en cada llamada, así que puede repreguntar y recordar.
  - HERRAMIENTAS: el modelo decide qué consultar (catálogo, cotizaciones, estado) en un bucle:
        piensa → llama herramienta → lee el resultado → piensa → responde.
  - CONFIANZA EN CÓDIGO, NO EN EL MODELO: el LLM solo PROPONE una compra. El dinero se mueve cuando
    TÚ confirmas en pantalla (s/n, lo maneja Python). Los datos de Pago Móvil nunca pasan por el LLM:
    la app los imprime directo desde la BD.

Uso:   python -m puente.concierge            (PayPal simulado)
       python -m puente.concierge --live     (PayPal Sandbox real + tasa real)
Env:   GEMINI_API_KEY + GEMINI_MODEL   (o OPENAI_API_KEY + OPENAI_MODEL)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import unicodedata
import uuid
from typing import Any, Callable, Protocol

from .config import Settings

MAX_TOOL_STEPS = 6      # tope de vueltas herramienta→modelo por mensaje del usuario
MAX_HISTORY = 40        # mensajes que se reenvían al modelo (además del system)
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Código SUDEBAN → (nombre, alias). Verifica contra la lista oficial antes de producción.
BANKS: dict[str, tuple[str, list[str]]] = {
    "0102": ("Banco de Venezuela", ["venezuela", "bdv"]),
    "0105": ("Mercantil", ["mercantil"]),
    "0108": ("Provincial", ["provincial", "bbva"]),
    "0134": ("Banesco", ["banesco"]),
    "0114": ("Bancaribe", ["bancaribe"]),
    "0116": ("BOD", ["bod", "occidental de descuento"]),
    "0163": ("Banco del Tesoro", ["tesoro"]),
    "0171": ("Banco Activo", ["activo"]),
    "0172": ("Bancamiga", ["bancamiga"]),
    "0175": ("Bicentenario", ["bicentenario"]),
    "0191": ("BNC", ["bnc", "nacional de credito"]),
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def resolve_bank(text: Any) -> str | None:
    """'Banesco' / 'banco de venezuela' / '0134' → código de 4 dígitos. None si no se reconoce (nunca adivina)."""
    t = _norm(str(text))
    if re.fullmatch(r"\d{4}", t):
        return t if t in BANKS else None
    for code, (name, aliases) in BANKS.items():
        if t == _norm(name) or any(re.search(rf"\b{re.escape(a)}\b", t) for a in aliases):
            return code
    return None


WAITING_FOR = {
    "AWAITING_PROVIDER_APPROVAL": "que el proveedor apruebe el pago en PayPal",
    "AWAITING_VES": "tu pago en bolívares por Pago Móvil",
    "VES_REPORTED": "que el proveedor confirme que recibió tu pago",
    "VES_CONFIRMED": "que se complete el cobro en PayPal",
    "AWAITING_DELIVERY": "la entrega de tu compra",
    "DELIVERED": "que confirmes que recibiste tu compra",
    "COMPLETED": "nada: la compra está completa",
    "HUMAN_REVIEW": "que un operador revise tu caso",
    "EXPIRED": "nada: la solicitud venció",
    "REFUNDED": "nada: la operación se revirtió",
    "FAILED": "nada: la operación no pudo realizarse",
}


def build_system_prompt(settings: Settings) -> str:
    if settings.lock_mode == "authorize":
        lock = ("Cuando el proveedor aprueba, PayPal retiene ese monto en su cuenta hasta que él confirme tus "
                "bolívares. Puedes decir 'retención', pero nunca 'garantizado al 100 %'.")
    else:
        lock = ("El proveedor se compromete al aprobar el pago en PayPal, pero eso NO retiene fondos. "
                "No uses las palabras 'escrow', 'retención' ni 'garantizado'.")
    return textwrap.dedent(f"""\
        Eres PUENTE, un asistente que ayuda a personas en Venezuela a pagar compras en dólares usando
        bolívares y Pago Móvil. Hablas español de Venezuela, tuteando (tú): cálido, claro y breve (máximo
        4-5 líneas salvo que pidan detalle). Nada de jerga técnica: nunca menciones score, request_id,
        JSON ni nombres de herramientas.

        CÓMO TRABAJAS
        1. Antes de actuar piensa qué te falta saber. Si falta algo (qué quiere comprar, con qué banco
           paga), PREGUNTA. Nunca asumas ni uses valores por defecto.
        2. Todo dato (productos, precios, tasa, proveedores) sale de tus herramientas. Jamás inventes ni
           "adivines" un producto parecido. Si lo que pide no está en el catálogo (por ejemplo Platzi),
           dilo con honestidad, muestra lo que sí hay y pregunta qué prefiere.
        3. Con las ofertas en mano, recomienda una y explica en lenguaje humano por qué (precio frente a
           confiabilidad: operaciones completadas, disputas, rapidez) y qué se sacrifica frente a la
           otra opción. Usa solo los números que te dan.
        4. Para avanzar con una compra llama propose_purchase. La app le pedirá confirmación al usuario
           en pantalla. Hasta que el sistema diga que se confirmó, NO afirmes que la compra está hecha.
        5. Tras confirmar, el proveedor debe aprobar el pago en PayPal; solo entonces la app muestra los
           datos de Pago Móvil. Tú nunca ves, pides ni repites datos de pago (teléfonos, cédulas,
           cuentas). Si preguntan por ellos, di que la app se los muestra cuando el proveedor apruebe.
        6. Cuando el usuario diga que ya pagó, pídele el número de referencia y usa report_payment.
           Para saber cómo va todo usa check_status.
        7. {lock}
        8. Esto es una demostración en el sandbox de PayPal y el pago en bolívares es simulado. Si te
           preguntan, dilo con claridad.""")


TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "list_catalog",
        "description": "Lista TODO lo que se puede comprar hoy. Úsala para saber si lo que pide el usuario existe.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_quotes",
        "description": "Cotiza un ítem del catálogo en bolívares con los mejores proveedores. Requiere item_id "
                       "(de list_catalog) y el banco desde el que el usuario va a pagar (nombre o código).",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "integer"},
            "bank": {"type": "string", "description": "Banco del usuario, ej. 'Banesco' o '0134'"}},
            "required": ["item_id", "bank"]}}},
    {"type": "function", "function": {
        "name": "propose_purchase",
        "description": "Propone comprar con la oferta elegida (rank 1, 2 o 3). NO compra: la app pide "
                       "confirmación al usuario en pantalla.",
        "parameters": {"type": "object", "properties": {"rank": {"type": "integer"}}, "required": ["rank"]}}},
    {"type": "function", "function": {
        "name": "check_status",
        "description": "Consulta cómo va la compra actual y qué se está esperando.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "report_payment",
        "description": "Registra que el usuario ya pagó en bolívares, con el número de referencia de su comprobante.",
        "parameters": {"type": "object", "properties": {
            "bank_reference": {"type": "string"},
            "amount_ves": {"type": "number", "description": "Solo si el usuario dice que pagó un monto distinto al cotizado"}},
            "required": ["bank_reference"]}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


# ───────────────────────────── adaptadores de LLM ─────────────────────────────
class ChatLLM(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        """Devuelve el mensaje del asistente en formato OpenAI: {role, content, tool_calls?}."""


class OpenAICompatLLM:
    """Sirve para OpenAI y para Gemini (endpoint compatible con OpenAI, en beta según Google)."""

    def __init__(self, *, api_key: str, model: str, base_url: str | None = None, temperature: float = 0.4):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model, self.temperature = model, temperature

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        r = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, tool_choice="auto", temperature=self.temperature)
        m = r.choices[0].message
        # model_dump conserva campos extra que el proveedor devuelva en tool_calls (hay que reenviarlos tal cual).
        calls = [tc.model_dump(exclude_none=True) for tc in (m.tool_calls or [])]
        return {"role": "assistant", "content": m.content, "tool_calls": calls or None}


def make_llm(provider: str | None = None) -> ChatLLM:
    provider = provider or os.getenv("LLM_PROVIDER") or ("gemini" if (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")) else "openai")
    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        model = os.getenv("GEMINI_MODEL")
        if not key or not model:
            raise SystemExit("Faltan GEMINI_API_KEY y GEMINI_MODEL en .env (usa el mismo modelo que ya usas en tu agents.py).")
        return OpenAICompatLLM(api_key=key, model=model, base_url=GEMINI_BASE_URL)
    key, model = os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_MODEL")
    if not key or not model:
        raise SystemExit("Faltan OPENAI_API_KEY y OPENAI_MODEL en .env.")
    return OpenAICompatLLM(api_key=key, model=model)


def _normalize_assistant(msg: dict) -> dict:
    """Garantiza ids y argumentos como string en las tool_calls, conservando cualquier campo extra."""
    msg = dict(msg)
    msg["role"] = "assistant"
    calls = []
    for tc in msg.get("tool_calls") or []:
        tc = dict(tc)
        tc["type"] = "function"
        tc["id"] = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
        fn = dict(tc.get("function") or {})
        args = fn.get("arguments", "{}")
        fn["arguments"] = args if isinstance(args, str) else json.dumps(args)
        tc["function"] = fn
        calls.append(tc)
    if calls:
        msg["tool_calls"] = calls
    else:
        msg.pop("tool_calls", None)
    return msg


# ───────────────────────────── el agente ─────────────────────────────
class Concierge:
    def __init__(self, orch, conn, llm: ChatLLM, settings: Settings | None = None, *, customer_id: int = 1,
                 show_provider_messages: bool = True, out: Callable[[str], None] = print,
                 trace: Callable[[str], None] | None = None):
        self.orch, self.conn, self.llm = orch, conn, llm
        self.s = settings or orch.s
        self.customer_id = customer_id
        self.show_provider_messages = show_provider_messages
        self.out, self.trace = out, trace or (lambda s: None)
        self.messages: list[dict] = [{"role": "system", "content": build_system_prompt(self.s)}]
        self.rid: int | None = None
        self.quotes: list[dict] = []
        self.pending: dict | None = None
        self._seen = conn.execute("SELECT COALESCE(MAX(id),0) FROM outbox").fetchone()[0]

    # --- conversación ---
    def turn(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        return self._run_llm()

    def _trimmed(self) -> list[dict]:
        system, rest = self.messages[0], self.messages[1:]
        if len(rest) > MAX_HISTORY:
            rest = rest[-MAX_HISTORY:]
            while rest and rest[0]["role"] != "user":      # nunca empezar en medio de una llamada a herramienta
                rest = rest[1:]
        return [system, *rest]

    def _run_llm(self) -> str:
        for _ in range(MAX_TOOL_STEPS):
            msg = _normalize_assistant(self.llm.complete(self._trimmed(), TOOLS))
            self.messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip()
            for call in calls:
                result = self._dispatch(call)
                self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                      "content": json.dumps(result, ensure_ascii=False, default=str)})
        return "Me enredé con esa consulta. ¿Me la repites con otras palabras?"

    def _dispatch(self, call: dict) -> dict:
        name = (call.get("function") or {}).get("name", "")
        raw = (call.get("function") or {}).get("arguments") or "{}"
        if name not in TOOL_NAMES:
            return {"error": f"herramienta desconocida: {name}"}
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            return {"error": "argumentos inválidos (se esperaba un objeto JSON)"}
        try:
            result = getattr(self, f"tool_{name}")(**args)
        except (ValueError, TypeError, KeyError) as e:
            result = {"error": f"no se pudo ejecutar {name}: {e}"}
        self.trace(f"{name}({', '.join(f'{k}={v!r}' for k, v in args.items())}) → "
                   f"{'ERROR' if 'error' in result else 'ok'}")
        return result

    # --- herramientas (lo único que el modelo puede hacer) ---
    def tool_list_catalog(self) -> dict:
        rows = self.conn.execute(
            "SELECT c.id, c.title, c.price_usd, m.name merchant, m.delivery_type FROM catalog_items c "
            "JOIN merchants m ON m.id=c.merchant_id WHERE c.active=1 AND c.billing_kind='one_time' ORDER BY c.id").fetchall()
        return {"items": [{"item_id": r["id"], "title": r["title"], "merchant": r["merchant"],
                           "price_usd": r["price_usd"],
                           "delivery": "digital" if r["delivery_type"] == "digital" else "físico"} for r in rows],
                "note": "Solo existen estos ítems. Si lo que pidió el usuario no está, díselo."}

    def tool_get_quotes(self, item_id: int, bank: str) -> dict:
        code = resolve_bank(bank)
        if code is None:
            return {"error": "banco no reconocido; pregúntale al usuario",
                    "known_banks": [n for n, _ in BANKS.values()]}
        res = self.orch.create_request(self.customer_id, int(item_id), code, f"chat-{uuid.uuid4().hex}", flow="p2p")
        if not res.ok:
            return {"error": res.message_es, "code": res.code}
        rid = res.data["request_id"]
        q = self.orch.quote(rid)
        if not q.ok:
            self.rid, self.quotes = None, []
            supported = sorted({b for r in self.conn.execute("SELECT accepts_banks FROM providers")
                                for b in json.loads(r[0])})
            return {"error": q.message_es, "code": q.code,
                    "banks_with_providers": [BANKS[b][0] for b in supported if b in BANKS]}
        self.rid, self.quotes = rid, q.data["quotes"]
        best = min(x["total_ves"] for x in self.quotes)
        offers = []
        for x in self.quotes:
            p = self.conn.execute("SELECT completed_orders c, disputed_orders d, failed_orders f, avg_settle_minutes m "
                                  "FROM providers WHERE id=?", (x["provider_id"],)).fetchone()
            offers.append({"rank": x["rank"], "provider": x["display_name"], "total_ves": x["total_ves"],
                           "rate_ves_per_usd": x["rate_ves_per_usd"],
                           "pct_more_than_cheapest": round((x["total_ves"] / best - 1) * 100, 1),
                           "completed_ops": p["c"], "disputed_ops": p["d"], "failed_ops": p["f"],
                           "avg_minutes_to_confirm": p["m"]})
        item = self.conn.execute("SELECT title, price_usd FROM catalog_items WHERE id=?", (int(item_id),)).fetchone()
        return {"item": item["title"], "amount_usd": item["price_usd"], "reference_rate": q.data["fx_rate"],
                "rate_is_cached": q.data["fx_fallback"], "offers": offers, "recommended_rank": 1,
                "providers_filtered_out": len(q.data["rejected"])}

    def tool_propose_purchase(self, rank: int) -> dict:
        rank = int(rank)
        offer = next((x for x in self.quotes if x["rank"] == rank), None)
        if self.rid is None or offer is None:
            return {"error": "primero cotiza (get_quotes) y elige una oferta existente"}
        item = self.conn.execute("SELECT c.title, c.price_usd FROM purchase_requests r JOIN catalog_items c "
                                 "ON c.id=r.item_id WHERE r.id=?", (self.rid,)).fetchone()
        self.pending = {"rid": self.rid, "rank": rank, "provider": offer["display_name"],
                        "total_ves": offer["total_ves"], "item": item["title"], "amount_usd": item["price_usd"]}
        return {"status": "awaiting_user_confirmation",
                "message": "La app le pedirá confirmación al usuario. NO digas que ya se compró."}

    def tool_check_status(self) -> dict:
        if self.rid is None:
            return {"error": "no hay una compra en curso"}
        state = self.conn.execute("SELECT state FROM purchase_requests WHERE id=?", (self.rid,)).fetchone()["state"]
        if state == "AWAITING_PROVIDER_APPROVAL":          # sin webhook: consultamos a PayPal
            self.orch.on_provider_locked(self.rid)
            state = self.conn.execute("SELECT state FROM purchase_requests WHERE id=?", (self.rid,)).fetchone()["state"]
        return {"state": state, "waiting_for": WAITING_FOR.get(state, state),
                "note": "Los datos de pago los muestra la app; nunca los pidas ni los repitas."}

    def tool_report_payment(self, bank_reference: str, amount_ves: float | None = None) -> dict:
        if self.rid is None:
            return {"error": "no hay una compra en curso"}
        q = self.conn.execute("SELECT q.total_ves FROM quotes q JOIN purchase_requests r ON r.selected_quote_id=q.id "
                              "WHERE r.id=?", (self.rid,)).fetchone()
        if q is None:
            return {"error": "todavía no se eligió proveedor"}
        amount = float(amount_ves) if amount_ves is not None else q["total_ves"]
        r = self.orch.report_ves_payment(self.rid, bank_reference=str(bank_reference), amount_ves=amount)
        return {"ok": r.ok, "state": r.state, "message": r.message_es, "code": r.code}

    # --- confirmación humana (código, no LLM) ---
    def confirm_pending(self):
        p, self.pending = self.pending, None
        res = self.orch.select_quote_p2p(p["rid"], p["rank"])
        self.messages.append({"role": "user", "content":
                              f"[SISTEMA] El usuario CONFIRMÓ la compra. Resultado: {res.message_es}"})
        return res

    def cancel_pending(self) -> None:
        self.pending = None
        self.messages.append({"role": "user", "content": "[SISTEMA] El usuario canceló la propuesta de compra."})

    def follow_up(self) -> str:
        """Deja que el modelo cierre el turno tras una confirmación o cancelación."""
        return self._run_llm()

    # --- mensajes de la app (aquí SÍ viajan los datos de Pago Móvil: directo a pantalla, nunca al LLM) ---
    def flush_outbox(self) -> list[str]:
        if self.rid is None:
            return []
        shown = []
        rows = self.conn.execute("SELECT id, to_role, body FROM outbox WHERE request_id=? AND id>? ORDER BY id",
                                 (self.rid, self._seen)).fetchall()
        for r in rows:
            self._seen = r["id"]
            if r["to_role"] == "customer":
                title = "📲 PUENTE → tú"
            elif r["to_role"] == "provider" and self.show_provider_messages:
                title = "📨 (demo) PUENTE → proveedor"
            else:
                continue
            block = f"\n{title}:\n" + textwrap.indent(r["body"], "   ")
            shown.append(block)
            self.out(block)
        return shown


# ───────────────────────────── consola ─────────────────────────────
BANNER = """━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🤖 Hola, soy PUENTE. Te ayudo a pagar en dólares usando bolívares.
   Escríbeme con tus palabras. Comandos: salir · /carlos aprobar · /carlos confirmar
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""
YES = {"s", "si", "sí", "y", "yes"}


def _carlos(c: Concierge, gw, cmd: str, out) -> None:
    """Atajos de demo para hacer el papel del proveedor desde la misma consola."""
    if c.rid is None or c.pending is not None:
        return out("   (no hay una compra confirmada todavía)")
    row = c.conn.execute("SELECT r.selected_quote_id sq, q.provider_id pid FROM purchase_requests r "
                         "LEFT JOIN quotes q ON q.id=r.selected_quote_id WHERE r.id=?", (c.rid,)).fetchone()
    if not row or row["pid"] is None:
        return out("   (aún no se eligió proveedor)")
    if cmd == "aprobar":
        if hasattr(gw, "approve"):
            oid = c.conn.execute("SELECT paypal_order_id FROM paypal_orders WHERE request_id=? ORDER BY id DESC LIMIT 1",
                                 (c.rid,)).fetchone()[0]
            gw.approve(oid)
            res = c.orch.on_provider_locked(c.rid)
            out(f"   Carlos aprobó (simulado) → {res.message_es}")
        else:
            out("   En modo --live el proveedor aprueba abriendo el link de PayPal sandbox; luego pregúntame cómo va.")
    elif cmd == "confirmar":
        res = c.orch.provider_confirm_ves(c.rid, row["pid"])
        out(f"   Carlos confirmó → {res.message_es}")
    else:
        out("   Usa: /carlos aprobar  o  /carlos confirmar")
    c.flush_outbox()


def run_repl(c: Concierge, gw=None, *, input_fn=input, out=print) -> None:
    out(BANNER)
    while True:
        try:
            text = input_fn("\n💬 Tú: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text.lower() in {"salir", "exit", "quit"}:
            break
        if text.lower().startswith("/carlos"):
            _carlos(c, gw, text.lower().replace("/carlos", "").strip(), out)
            continue
        reply = c.turn(text)
        if reply:
            out(f"\n🤖 PUENTE: {reply}")
        c.flush_outbox()
        if c.pending:
            p = c.pending
            ans = input_fn(f"\n🔐 ¿Confirmas pagar Bs. {p['total_ves']:,.2f} por «{p['item']}» "
                           f"(US$ {p['amount_usd']:.2f}) con {p['provider']}? (s/n): ").strip().lower()
            if ans in YES:
                res = c.confirm_pending()
                out(f"\n✅ {res.message_es}")
                c.flush_outbox()
            else:
                c.cancel_pending()
                out("\n↩️  Cancelado. No se movió nada.")
            follow = c.follow_up()
            if follow:
                out(f"\n🤖 PUENTE: {follow}")
            c.flush_outbox()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="puente-concierge")
    ap.add_argument("--live", action="store_true", help="PayPal Sandbox real y tasa real")
    ap.add_argument("--provider", choices=["gemini", "openai"])
    ap.add_argument("--quiet", action="store_true", help="no mostrar qué herramientas usa el agente")
    ap.add_argument("--hide-provider", action="store_true", help="no mostrar los mensajes al proveedor")
    a = ap.parse_args(argv)
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    from . import cli
    if a.live:
        cli.LIVE_MODE = True
    conn, orch, gw, _ = cli.build(mock=not a.live)
    llm = make_llm(a.provider)
    c = Concierge(orch, conn, llm, show_provider_messages=not a.hide_provider,
                  trace=None if a.quiet else (lambda s: print(f"   ⚙ {s}")))
    run_repl(c, gw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
