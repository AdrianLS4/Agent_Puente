"""Pruebas del Concierge con un LLM de guion. NO prueban la inteligencia del modelo (eso se ve con la API key real);
prueban lo que el CÓDIGO garantiza pase lo que pase con el modelo: sin compras sin confirmar, sin datos de pago
en el LLM, sin valores por defecto, y sin que una herramienta mal llamada rompa el bucle."""
import json
import re

from puente.cli import build
from puente.concierge import MAX_TOOL_STEPS, Concierge, resolve_bank, run_repl


class FakeLLM:
    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def complete(self, messages, tools):
        self.calls += 1
        return self.script.pop(0)


def call(name, raw=None, **args):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "", "type": "function", "function": {"name": name, "arguments": raw if raw is not None else json.dumps(args)}}]}


def say(text):
    return {"role": "assistant", "content": text}


def make(script, **kw):
    conn, orch, gw, clock = build(mock=True)
    shown = []
    c = Concierge(orch, conn, FakeLLM(script), out=shown.append, **kw)
    return c, conn, gw, shown


def tool_results(c):
    return [json.loads(m["content"]) for m in c.messages if m["role"] == "tool"]


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_resolve_bank_never_guesses():
    assert resolve_bank("Banesco") == "0134" and resolve_bank("banco de venezuela") == "0102"
    assert resolve_bank("BBVA Provincial") == "0108" and resolve_bank("0105") == "0105"
    assert resolve_bank("Banco Imaginario") is None and resolve_bank("9999") is None and resolve_bank("") is None


def test_unknown_product_creates_nothing_and_hides_recurring_item():
    c, conn, _, _ = make([call("list_catalog"), say("No tengo Platzi; sí tengo estos cursos…")])
    reply = c.turn("quiero un curso en platzi")
    assert "Platzi" in reply and count(conn, "purchase_requests") == 0 and c.pending is None
    titles = [i["title"] for i in tool_results(c)[0]["items"]]
    assert titles and not any("mensual" in t.lower() for t in titles)


def test_unknown_bank_is_an_error_not_a_default():
    c, conn, _, _ = make([call("get_quotes", item_id=1, bank="Banco Imaginario"), say("¿Con qué banco pagas?")])
    c.turn("el curso de python")
    assert "error" in tool_results(c)[0] and count(conn, "purchase_requests") == 0


def test_quotes_never_expose_payment_data():
    c, conn, _, _ = make([call("get_quotes", item_id=1, bank="Banesco"), say("Te recomiendo la 1")])
    c.turn("python con banesco")
    raw = " ".join(m["content"] for m in c.messages if m["role"] == "tool")
    assert not re.search(r"0414\d{7}|@sandbox\.test|V-0000", raw)
    res = tool_results(c)[0]
    assert len(res["offers"]) == 3 and res["offers"][0]["completed_ops"] >= 0 and count(conn, "purchase_requests") == 1


def test_recurring_item_is_rejected():
    c, conn, _, _ = make([])
    rid = conn.execute("SELECT id FROM catalog_items WHERE billing_kind='recurring'").fetchone()[0]
    c.llm.script = [call("get_quotes", item_id=rid, bank="Banesco"), say("no puedo con cobros recurrentes")]
    c.turn("el plan mensual")
    assert tool_results(c)[0]["code"] == "E_RECURRING_UNSUPPORTED"


def test_proposal_moves_no_money_until_code_confirms():
    c, conn, gw, _ = make([call("get_quotes", item_id=1, bank="Banesco"), call("propose_purchase", rank=1),
                           say("¿Confirmas?")])
    c.turn("python con banesco, la mejor opción")
    assert c.pending and count(conn, "paypal_orders") == 0 and gw.calls == []
    res = c.confirm_pending()
    assert res.ok and count(conn, "paypal_orders") == 1 and c.pending is None
    assert c.messages[-1]["content"].startswith("[SISTEMA] El usuario CONFIRMÓ")


def test_cancel_clears_pending_and_creates_no_order():
    c, conn, gw, _ = make([call("get_quotes", item_id=1, bank="Banesco"), call("propose_purchase", rank=2), say("ok?")])
    c.turn("python con banesco")
    c.cancel_pending()
    assert c.pending is None and count(conn, "paypal_orders") == 0 and gw.calls == []


def test_propose_rejects_rank_that_was_never_offered():
    c, conn, _, _ = make([call("get_quotes", item_id=1, bank="Banesco"), call("propose_purchase", rank=9), say("hm")])
    c.turn("python con banesco")
    assert "error" in tool_results(c)[1] and c.pending is None


def test_payment_instructions_reach_the_screen_but_not_the_llm():
    c, conn, gw, shown = make([call("get_quotes", item_id=1, bank="Banesco"), call("propose_purchase", rank=1), say("¿ok?")])
    c.turn("python con banesco")
    c.confirm_pending()
    oid = conn.execute("SELECT paypal_order_id FROM paypal_orders").fetchone()[0]
    gw.approve(oid)
    c.llm.script = [call("check_status"), say("Ya está aprobado; revisa tu pantalla")]
    c.turn("¿ya aprobó?")
    assert tool_results(c)[-1]["state"] == "AWAITING_VES"
    on_screen = " ".join(c.flush_outbox() + shown)
    assert re.search(r"0414\d{7}", on_screen)                                   # el usuario SÍ los ve…
    everything_llm_saw = " ".join(str(m.get("content")) for m in c.messages)
    assert not re.search(r"0414\d{7}|V-0000", everything_llm_saw)               # …pero el LLM NO


def test_report_payment_defaults_to_quoted_amount():
    c, conn, gw, _ = make([call("get_quotes", item_id=1, bank="Banesco"), call("propose_purchase", rank=1), say("ok")])
    c.turn("python con banesco")
    c.confirm_pending()
    gw.approve(conn.execute("SELECT paypal_order_id FROM paypal_orders").fetchone()[0])
    c.llm.script = [call("check_status"), say("aprobado"), ]
    c.turn("¿ya?")
    c.llm.script = [call("report_payment", bank_reference="00123456"), say("Listo, el proveedor lo verifica")]
    c.turn("ya pagué, la ref es 00123456")
    assert tool_results(c)[-1]["state"] == "VES_REPORTED"


def test_bad_arguments_unknown_tool_and_runaway_loop_do_not_break_the_agent():
    c, conn, _, _ = make([call("get_quotes", raw="{no es json"), call("hack_paypal"),
                          call("get_quotes", raw='{"item_id": "abc", "bank": "Banesco"}'), say("perdón, ¿me lo repites?")])
    assert "repites" in c.turn("hola")
    errs = [r for r in tool_results(c) if "error" in r]
    assert len(errs) == 3 and count(conn, "purchase_requests") == 0
    c2, _, _, _ = make([call("list_catalog")] * 20)
    assert "enredé" in c2.turn("hola") and c2.llm.calls == MAX_TOOL_STEPS


def test_history_window_never_starts_in_the_middle_of_a_tool_call():
    c, *_ = make([])
    for i in range(30):
        c.messages += [{"role": "user", "content": f"u{i}"}, call("list_catalog") | {"tool_calls": [
            {"id": f"x{i}", "type": "function", "function": {"name": "list_catalog", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"x{i}", "content": "{}"}, say("a")]
    window = c._trimmed()
    assert window[0]["role"] == "system" and window[1]["role"] == "user" and len(window) <= 41


def test_repl_asks_for_confirmation_and_respects_a_no():
    c, conn, gw, _ = make([call("get_quotes", item_id=1, bank="Banesco"), call("propose_purchase", rank=1),
                           say("¿Lo hacemos?"), say("Sin problema, no se movió nada.")])
    inputs = iter(["quiero python con banesco", "n", "salir"])
    printed = []
    run_repl(c, gw, input_fn=lambda _p="": next(inputs), out=printed.append)
    assert count(conn, "paypal_orders") == 0 and any("Cancelado" in p for p in printed)
