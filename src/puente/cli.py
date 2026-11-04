"""CLI de demo.   python -m puente.cli demo [escenario|all]   ·   errors   ·   live-check"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from .config import Settings
from .db import connect, init_db, iso, utcnow
from .errors import catalog_markdown
from .fx import http_fetcher, mock_fetcher
from .orchestrator import Orchestrator, StepResult
from .paypal_gateway import LivePayPal, MockPayPal, PayPalError
from .seed import seed


class Clock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float = 0, hours: float = 0) -> None:
        self.now += timedelta(minutes=minutes, hours=hours)


def say(step: str, r: StepResult) -> StepResult:
    flag = "✓" if r.ok else "✗"
    code = f" [{r.code}]" if r.code else ""
    print(f"  {flag} {step:<28} → {r.state}{code}  {r.message_es}")
    return r


BRAIN = None   # se activa con `demo --llm` (requiere OPENAI_API_KEY)
LIVE_MODE = False


def build(brain=None, settings: Settings | None = None, mock: bool | None = None, **orch_kw):
    s = settings or Settings()
    is_mock = mock if mock is not None else (s.mock_mode and not LIVE_MODE)
    conn = connect(":memory:")
    init_db(conn)
    seed(conn)
    if is_mock:
        clock, gw = Clock(), MockPayPal()
        fetcher = mock_fetcher(40.0)
        sleep_fn = lambda s: None
    else:
        clock = utcnow
        gw = LivePayPal(s.paypal_client_id, s.paypal_secret, sandbox=s.paypal_sandbox)
        fetcher = http_fetcher(s.fx_url, s.fx_json_path) if s.fx_url else mock_fetcher(40.0)
        sleep_fn = time.sleep
    active_brain = brain or BRAIN
    if active_brain is None and not is_mock and (s.gemini_api_key or s.openai_api_key):
        from .agents import AgentBrain
        active_brain = AgentBrain(s)
    orch = Orchestrator(conn, gw, s, fetcher, brain=active_brain, sleep=sleep_fn,
                        clock=clock, **orch_kw)
    return conn, orch, gw, clock


def start(orch: Orchestrator, key: str, item_id: int = 1, pick: int = 1) -> int:
    rid = say("crear solicitud", orch.create_request(1, item_id, "0102", key)).data["request_id"]
    q = say("cotizar + ranking", orch.quote(rid))
    print(f"      tasa ref: {q.data.get('fx_rate')} Bs/USD (fuente: {'respaldo' if q.data.get('fx_fallback') else 'en vivo'})")
    for x in q.data.get("quotes", []):
        print(f"      #{x['rank']} {x['display_name']:<30} score {x['score']:>5}  total {x['total_ves']:>9} Bs.")
    print(f"      descartados: {q.data.get('rejected')}")
    say("elegir cotización", orch.select_quote(rid, pick))
    return rid


def pay_and_capture(orch, gw, rid) -> None:
    q = orch._row("SELECT total_ves FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)", rid)
    say("pago Bs. confirmado", orch.confirm_ves(rid, f"REF{rid}001", q["total_ves"]))
    o = say("crear orden PayPal", orch.create_paypal_order(rid))
    if isinstance(gw, MockPayPal):
        gw.approve(o.data["paypal_order_id"])                      # el proveedor aprueba en PayPal sandbox
    else:
        print(f"\n      [SANDBOX] Orden real creada: {o.data['paypal_order_id']}")
        print(f"      [SANDBOX] Abrí este link con tu cuenta Personal de Sandbox para aprobar:")
        print(f"      >>> {o.data['approve_url']}\n")
        input("      Presioná [ENTER] una vez que hayas aprobado en el navegador...")
    say("capturar", orch.capture(rid))


def sc_happy():
    """Flujo feliz (comercio digital)."""
    _, orch, gw, _ = build()
    rid = start(orch, "k-happy")
    pay_and_capture(orch, gw, rid)
    say("entrega digital", orch.record_delivery(rid, proof="LICENCIA-ABC-123", delivered=True))
    say("cliente confirma", orch.confirm_receipt(rid))


def sc_retries():
    """Errores transitorios de PayPal: 429 x2 en create_order, y rechazo de instrumento en la captura."""
    _, orch, gw, _ = build()
    rid = start(orch, "k-retry")
    gw.fail_next["create_order"] = [PayPalError(429, "RATE_LIMIT_REACHED"), PayPalError(429, "RATE_LIMIT_REACHED")]
    q = orch._row("SELECT total_ves FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)", rid)
    say("pago Bs. confirmado", orch.confirm_ves(rid, "REFR001", q["total_ves"]))
    o = say("crear orden (con 429s)", orch.create_paypal_order(rid))
    gw.approve(o.data["paypal_order_id"])
    gw.fail_next["capture_order"] = [PayPalError(422, "UNPROCESSABLE_ENTITY", "INSTRUMENT_DECLINED")]
    say("capturar (tarjeta rechazada)", orch.capture(rid))
    say("capturar (reintento)", orch.capture(rid))
    n = orch.conn.execute("SELECT COUNT(*) c FROM error_events").fetchone()["c"]
    print(f"      error_events registrados: {n}")


def sc_failover():
    """El proveedor no aprueba el pago: se devuelven Bs., se penaliza y se reabre con otro."""
    conn, orch, gw, clock = build()
    rid = start(orch, "k-fo")
    q = orch._row("SELECT total_ves, provider_id FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)", rid)
    say("pago Bs. confirmado", orch.confirm_ves(rid, "REFFO001", q["total_ves"]))
    say("crear orden PayPal", orch.create_paypal_order(rid))
    clock.advance(minutes=16)
    for rid_, st, code in orch.check_timeouts():
        print(f"      scheduler: solicitud {rid_} ({st}) → {code}")
    child = conn.execute("SELECT id, excluded_providers, state FROM purchase_requests WHERE parent_request_id=?", (rid,)).fetchone()
    print(f"      solicitud hija #{child['id']} excluye proveedores {child['excluded_providers']}")
    say("cotizar (hija)", orch.quote(child["id"]))
    failed = conn.execute("SELECT display_name, failed_orders FROM providers WHERE id=?", (q["provider_id"],)).fetchone()
    print(f"      penalización: {failed['display_name']} → failed_orders={failed['failed_orders']}")


def sc_delivery_timeout():
    """Pagado pero el comercio no entrega: reembolso PayPal + devolución en Bs."""
    _, orch, gw, clock = build()
    rid = start(orch, "k-dt")
    pay_and_capture(orch, gw, rid)
    clock.advance(hours=25)
    for rid_, st, code in orch.check_timeouts():
        print(f"      scheduler: solicitud {rid_} ({st}) → {code}")
    print("      llamadas a PayPal:", [c[0] for c in gw.calls])


def _dispute(item_id: int, delivered: bool, hours: float = 72):
    _, orch, gw, clock = build()
    rid = start(orch, f"k-disp-{item_id}-{delivered}", item_id=item_id)
    pay_and_capture(orch, gw, rid)
    if delivered:
        say("entrega digital", orch.record_delivery(rid, proof="LICENCIA-XYZ", delivered=True))
    r = say("disputa abierta", orch.open_dispute(rid, f"PP-D-{rid}", "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                                                 clock() + timedelta(hours=hours)))
    d = orch.conn.execute("SELECT decision, decided_by, proof_strength FROM disputes WHERE request_id=?", (rid,)).fetchone()
    print(f"      decisión={d['decision']} por={d['decided_by']} prueba={d['proof_strength']}")
    return orch, rid


def sc_dispute_contest():
    """Disputa con prueba de entrega fuerte → el agente contesta con evidencia."""
    orch, rid = _dispute(1, delivered=True)
    say("PayPal resuelve: ganada", orch.resolve_dispute_outcome(rid, "won"))


def sc_dispute_accept():
    """Disputa sin prueba y monto bajo → aceptar y devolver."""
    _dispute(1, delivered=False)


def sc_dispute_human():
    """Monto alto ($120 > $50) → expediente listo y escalado a humano."""
    orch, _ = _dispute(2, delivered=False)
    print("      cola humana:", [dict(r) for r in orch.conn.execute("SELECT reason_code, status FROM human_review_queue")])


def sc_invoice():
    """Flujo de facturación B2B (Invoicing v2): creación, envío, pago y entrega."""
    _, orch, gw, _ = build()
    rid = start(orch, "k-invoice", item_id=2)
    q = orch._row("SELECT total_ves FROM quotes WHERE id=(SELECT selected_quote_id FROM purchase_requests WHERE id=?)", rid)
    say("pago Bs. confirmado", orch.confirm_ves(rid, f"REFINV{rid}", q["total_ves"]))
    inv = say("crear y enviar factura", orch.create_paypal_invoice(rid))
    if isinstance(gw, MockPayPal):
        gw.pay_invoice(inv.data["paypal_invoice_id"])
    else:
        print(f"\n      [SANDBOX] Factura enviada: {inv.data['paypal_invoice_id']}")
        print(f"      [SANDBOX] Pagar factura en este link:")
        print(f"      >>> {inv.data['invoice_url']}\n")
        input("      Presioná [ENTER] una vez que hayas pagado la factura en el navegador...")
    say("verificar pago factura", orch.check_invoice_paid(rid))
    say("entrega digital", orch.record_delivery(rid, proof="SAAS-TOKEN-1234", delivered=True))
    say("cliente confirma", orch.confirm_receipt(rid))


def print_pago_movil_ticket(ticket: dict) -> None:
    print("\n      ┌──────────────────────────────────────────────────────────────┐")
    print("      │ 🔒 TICKET DE PAGO MÓVIL (Dólares retenidos en PayPal Escrow) │")
    print("      ├──────────────────────────────────────────────────────────────┤")
    print(f"      │ Proveedor:    {ticket['provider_name']:<46} │")
    print(f"      │ Banco:        {ticket['bank_name']:<46} │")
    print(f"      │ Teléfono:     {ticket['phone']:<46} │")
    print(f"      │ C.I.:         {ticket['id_doc']:<46} │")
    print(f"      │ Titular:      {ticket['holder_name']:<46} │")
    print(f"      │ Monto exacto: {ticket['total_ves']:>10.2f} Bs.                            │")
    print(f"      │ Concepto:     {ticket['concept_code']:<46} │")
    print(f"      │ Escrow ID:    {str(ticket['authorization_id']):<46} │")
    print("      └──────────────────────────────────────────────────────────────┘\n")


def sc_escrow():
    """Flujo P2P con Escrow (estilo Binance): retener USD -> Ticket Pago Móvil -> Pagar Bs -> Liberar."""
    _, orch, gw, _ = build()
    rid = start(orch, "k-escrow", item_id=1)

    # 1. Crear orden con intent AUTHORIZE
    o = say("crear orden Escrow USD", orch.create_escrow_order(rid))
    oid = o.data["paypal_order_id"]
    if isinstance(gw, MockPayPal):
        gw.approve(oid)
    else:
        print(f"\n      [SANDBOX] Orden de retención creada: {oid}")
        print(f"      [SANDBOX] El proveedor aprueba la retención en PayPal:")
        print(f"      >>> {o.data['approve_url']}\n")
        input("      Presioná [ENTER] una vez que hayas aprobado en el navegador...")

    # 2. Bloquear USD en custodia y emitir ticket de Pago Móvil
    lock = say("bloquear USD en custodia", orch.lock_usd_escrow(rid))
    ticket = lock.data["ticket"]
    print_pago_movil_ticket(ticket)

    # 3. El comprador hace el Pago Móvil
    ref = f"REF{rid}9988"
    if not isinstance(gw, MockPayPal):
        ref_in = input(f"      Ingresá el número de referencia del Pago Móvil [{ref}]: ").strip()
        if ref_in:
            ref = ref_in
    say("pago Bs. registrado", orch.confirm_ves(rid, ref, ticket["total_ves"]))

    # 4. El proveedor confirma que recibió los Bolívares (botón estilo Binance)
    if not isinstance(gw, MockPayPal):
        input("\n      [PROVEEDOR] Presioná [ENTER] para confirmar que recibiste los Bs. (Liberar dólares)...")
    say("proveedor libera fondos", orch.provider_release_and_capture(rid))

    # 5. Entrega y confirmación
    say("entrega digital", orch.record_delivery(rid, proof="LICENCIA-ESCROW-2026", delivered=True))
    say("cliente confirma recepción", orch.confirm_receipt(rid))


def interactive_chat():
    """Modo conversacional interactivo con IA (asistente PUENTE en terminal)."""
    conn, orch, gw, _ = build()
    print("━" * 65)
    print("🤖 ¡Hola! Soy PUENTE, tu agente de compras P2P asistido por IA.")
    print("━" * 65)
    print("Escribí libremente lo que querés comprar y cómo deseás pagar:")
    print("Ejemplos:")
    print("  • 'Hola, quiero comprar el curso de Python de 29 dólares con Banco de Venezuela'")
    print("  • 'Quiero una suscripción SaaS de 120$ y pago con Banco Activo'")
    print("  • 'Comprar teclado mecánico de 45$ con Banesco'")
    print("━" * 65)

    user_text = input("\n💬 Escribí acá tu mensaje: ").strip()
    if not user_text:
        user_text = "Quiero el curso de Python de 29 dólares y pago con Banco de Venezuela"
        print(f"   (Entrada por defecto: '{user_text}')")

    key = f"chat-{int(time.time())}"
    print(f"\n🧠 Analizando tu mensaje con Inteligencia Artificial (Gemini)...")
    req_res = orch.create_request_from_text(1, user_text, key)
    if not req_res.ok:
        print(f"❌ Error al procesar solicitud: {req_res.message_es}")
        return

    rid = req_res.data["request_id"]
    intent = req_res.data.get("parsed_intent", {})
    matched_item = req_res.data.get("matched_item", {})

    print(f"\n✅ Intención detectada:")
    print(f"   • Producto: {matched_item.get('title', 'Item')} (${matched_item.get('price_usd', 0):.2f} USD)")
    print(f"   • Banco detectado: {intent.get('customer_bank') or '0102 (Por defecto)'}")

    print(f"\n📈 Cotizando en tiempo real con tasa de mercado...")
    q_res = orch.quote(rid)
    if not q_res.ok:
        print(f"\n❌ Error al cotizar: {q_res.message_es}")
        rejected = q_res.data.get("rejected", {})
        if "bank_unsupported" in rejected.values():
            print(f"   El banco ingresado no tiene proveedores activos en este momento.")
        return

    fx_rate = q_res.data.get("fx_rate", 0)
    print(f"   Tasa de cambio de referencia: {fx_rate:.2f} Bs/USD (en vivo)")

    quotes = q_res.data.get("quotes", [])
    print(f"\nProveedores de liquidez rankeados:")
    for x in quotes:
        print(f"  #{x['rank']} {x['display_name']:<30} Total: {x['total_ves']:>10.2f} Bs.  Score: {x['score']:.1f}/100")

    ai_exp = q_res.data.get("ai_explanation")
    if ai_exp:
        print(f"\n🤖 Asesoría de IA (Gemini):")
        print(f"   \"{ai_exp}\"")

    orch.select_quote(rid, 1)
    print(f"\n✅ Proveedor #1 seleccionado para la operación.")

    print(f"\n⏳ Creando orden de retención (Escrow) en PayPal...")
    escrow = orch.create_escrow_order(rid)
    if not escrow.ok:
        print(f"❌ Error al crear orden en PayPal: {escrow.message_es}")
        return

    oid = escrow.data["paypal_order_id"]
    if isinstance(gw, MockPayPal):
        gw.approve(oid)
    else:
        print(f"\n🔗 [SANDBOX] Link de PayPal para que el proveedor retenga los fondos:")
        print(f"   >>> {escrow.data['approve_url']}\n")
        input("   Presioná [ENTER] una vez que hayas aprobado en PayPal Sandbox...")

    print("\n🔒 Bloqueando dólares en PayPal...")
    locked = orch.lock_usd_escrow(rid)
    ticket = locked.data["ticket"]
    print_pago_movil_ticket(ticket)

    ref = input(f"📲 Ingresá el número de comprobante/referencia de tu Pago Móvil [REF-{rid}9988]: ").strip() or f"REF-{rid}9988"
    orch.confirm_ves(rid, ref, ticket["total_ves"])
    print(f"\n✅ Pago Móvil registrado con referencia: {ref}")

    if not isinstance(gw, MockPayPal):
        input("\n[PROVEEDOR P2P] Presioná [ENTER] en la consola para confirmar recepción de Bolívares y liberar dólares...")
    else:
        print("👤 Proveedor revisó su cuenta bancaria y confirmó recepción de Bolívares.")

    print("\n💸 Liberando autorización de PayPal y pagando al comercio...")
    orch.provider_release_and_capture(rid)

    print("\n📦 Comercio procesando entrega digital...")
    orch.record_delivery(rid, proof="LICENCIA-ACTIVADA-2026", delivered=True)
    orch.confirm_receipt(rid)

    print("━" * 65)
    print("🎉 ¡COMPRA EXITOSA! Tu licencia digital ha sido entregada:")
    print("   🔑 LICENCIA-ACTIVADA-2026")
    print("━" * 65)


SCENARIOS = {
    "escrow": sc_escrow,
    "happy": sc_happy,
    "invoice": sc_invoice,
    "retries": sc_retries,
    "failover": sc_failover,
    "delivery-timeout": sc_delivery_timeout,
    "dispute-contest": sc_dispute_contest,
    "dispute-accept": sc_dispute_accept,
    "dispute-human": sc_dispute_human,
}


def live_check() -> int:
    """Día 1: confirma credenciales sandbox y crea una orden REAL de prueba vía el Agent Toolkit."""
    s = Settings()
    if not (s.paypal_client_id and s.paypal_secret):
        print("Faltan PAYPAL_CLIENT_ID / PAYPAL_SECRET en .env (créalas en developer.paypal.com → Apps & Credentials → Sandbox).")
        return 1
    from .paypal_gateway import LivePayPal
    gw = LivePayPal(s.paypal_client_id, s.paypal_secret, sandbox=True)
    out = gw.create_order(amount_usd=1.00, item_name="PUENTE live-check", reference_id="LIVE-CHECK")
    print("orden creada:", out["order_id"], out["status"])
    print("aprobar con la cuenta sandbox PERSONAL (proveedor):", out["approve_url"])
    return 0


def main(argv=None) -> int:
    global LIVE_MODE, BRAIN
    p = argparse.ArgumentParser(prog="puente")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("scenario", nargs="?", default="all", choices=["all", *SCENARIOS])
    d.add_argument("--llm", action="store_true", help="usa el AgentBrain (OpenAI Agents SDK) en disputas")
    d.add_argument("--live", action="store_true", help="usa PayPal Sandbox real y tasa real de mercado")
    sub.add_parser("errors")
    sub.add_parser("live-check")
    ch = sub.add_parser("chat", help="inicia el chat interactivo con PUENTE y Pago Móvil")
    ch.add_argument("--live", action="store_true", help="usa PayPal Sandbox real y tasa real de mercado")
    srv = sub.add_parser("serve", help="inicia el servidor de webhooks y scheduler de timeouts")
    srv.add_argument("--host", default="127.0.0.1", help="host donde escuchar")
    srv.add_argument("--port", type=int, default=8000, help="puerto donde escuchar")
    a = p.parse_args(argv)
    if a.cmd == "demo":
        s = Settings()
        if a.live or not s.mock_mode:
            LIVE_MODE = True
            if a.scenario == "all":
                print("━━ Modo LIVE detectado: ejecutando flujo 'happy' contra PayPal Sandbox real y tasa real")
                a.scenario = "happy"
        if a.llm:
            from .agents import AgentBrain
            BRAIN = AgentBrain(s)
    if a.cmd == "errors":
        print("# Catálogo de errores\n")
        print(catalog_markdown())
        return 0
    if a.cmd == "live-check":
        return live_check()
    if a.cmd == "chat":
        s = Settings()
        if a.live or not s.mock_mode:
            LIVE_MODE = True
        interactive_chat()
        return 0
    if a.cmd == "serve":
        import uvicorn
        print(f"━━ Iniciando servidor de webhooks y scheduler en http://{a.host}:{a.port}")
        print("   Webhooks endpoint: POST /webhooks/paypal")
        print("   Scheduler tick:    POST /scheduler/tick")
        print("   Health endpoint:   GET  /health")
        uvicorn.run("puente.server:app", host=a.host, port=a.port, reload=False)
        return 0
    for name in (SCENARIOS if a.scenario == "all" else [a.scenario]):
        print(f"\n━━ {name}: {SCENARIOS[name].__doc__}")
        SCENARIOS[name]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
