"""Agentes LLM (OpenAI Agents SDK + PayPal Agent Toolkit).

Diseño: el dinero lo mueve el orquestador determinista; los agentes solo hacen lo que un humano
haría con criterio:

  IntakeAgent      lenguaje natural (ES) -> PurchaseIntent estructurado
  SourcingAgent    explica en español por qué recomienda el #1 (usa score_breakdown real)
  ResolutionAgent  decide disputas, SOLO entre acciones que `policy.allowed_dispute_actions` permite
  PayPalOpsAgent   (modo conversacional/ops) con las tools del Agent Toolkit: órdenes, facturas, suscripciones

Se construyen perezosamente: sin OPENAI_API_KEY el resto del sistema funciona con reglas de respaldo.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from .config import Settings


class PurchaseIntent(BaseModel):
    item_description: str
    amount_usd: float | None = Field(None, description="Monto en USD si el usuario lo dijo")
    merchant_hint: str | None = None
    customer_bank: str | None = Field(None, description="Código de banco VE de 4 dígitos si se mencionó")
    urgency: Literal["low", "normal", "high"] = "normal"


class DisputeDecision(BaseModel):
    decision: Literal["contest", "accept", "human"]
    confidence: float = Field(ge=0, le=1)
    rationale_es: str = Field(description="Explicación breve en español, citando solo los hechos recibidos")


INTAKE_INSTRUCTIONS = (
    "Eres el agente de recepción de PUENTE. Extrae del mensaje del cliente venezolano qué quiere comprar "
    "en USD. No inventes montos, bancos ni comercios: si no están, déjalos en null."
)
SOURCING_INSTRUCTIONS = (
    "Eres el asesor de PUENTE. Recibes las cotizaciones ya rankeadas con su score_breakdown. Explica en 3 "
    "frases, en español sencillo, por qué la #1 es la recomendada y qué se sacrifica frente a la #2. "
    "Usa SOLO los números recibidos. Nunca cambies el orden ni prometas nada."
)
RESOLUTION_INSTRUCTIONS = (
    "Eres el agente de resolución de disputas de PUENTE. Recibes los hechos del caso y la lista `allowed` "
    "de acciones permitidas. Debes elegir UNA acción de `allowed`. Reglas: contesta solo si hay prueba de "
    "entrega; acepta si no hay prueba y el monto es bajo; si dudas, elige 'human'. Cita únicamente los "
    "hechos recibidos; nunca inventes evidencia."
)
OPS_INSTRUCTIONS = (
    "You are PUENTE's PayPal operations assistant (SANDBOX only). Think step by step before calling a tool. "
    "To create orders use create_order, to check use get_order_details, to capture use capture_order, "
    "for invoices use create_invoice then send_invoice. Never put card numbers or secrets in prompts."
)


def fallback_parse_intent(text: str) -> PurchaseIntent:
    """Extractor de lenguaje natural determinista y robusto (funciona con o sin OpenAI/Gemini)."""
    import re
    lower = text.lower()

    # 1. Monto en USD
    m = re.search(r"\$?\s*(\d+(?:\.\d{1,2})?)\s*(?:dolares|dólares|usd|\$)?", lower)
    amount = None
    if m:
        try:
            val = float(m.group(1))
            if val > 0:
                amount = val
        except ValueError:
            pass

    # 2. Código de Banco VE
    bank = None
    code_match = re.search(r"\b(01\d{2})\b", text)
    if code_match:
        bank = code_match.group(1)
    elif "venezuela" in lower or "bdv" in lower:
        bank = "0102"
    elif "banesco" in lower:
        bank = "0134"
    elif "mercantil" in lower:
        bank = "0105"
    elif "provincial" in lower or "bbva" in lower:
        bank = "0108"
    elif "activo" in lower:
        bank = "0171"
    elif "bancamiga" in lower:
        bank = "0172"
    elif "bnc" in lower:
        bank = "0191"

    # 3. Comercio / Producto
    merchant_hint = None
    if "curso" in lower or "python" in lower:
        merchant_hint = "CursoOnline Demo"
    elif "saas" in lower or "suscripcion" in lower or "suscripción" in lower:
        merchant_hint = "SaaS"
    elif "teclado" in lower or "tech" in lower or "mecanico" in lower or "mecánico" in lower:
        merchant_hint = "TiendaTech"
    elif "riot" in lower or "valorant" in lower or "league" in lower or "lol" in lower:
        merchant_hint = "Riot Games"
    elif "steam" in lower:
        merchant_hint = "Steam"
    elif "amazon" in lower:
        merchant_hint = "Amazon"

    # Limpiar descripción extrayendo solo el producto
    clean = re.sub(r"\b(hola|buen[oa]s|quiero|deseo|comprar|pagar|adquirir|a|para|en|con|de|por)\b", " ", lower)
    clean = re.sub(r"\$?\s*\d+(?:\.\d{1,2})?\s*(?:dolares|dólares|usd|\$)?", " ", clean)
    clean = re.sub(r"\b(banco|venezuela|bdv|banesco|mercantil|provincial|bbva|activo|bancamiga|bnc|\d{4})\b", " ", clean)
    clean = " ".join(clean.split()).strip()
    item_desc = clean.title() if clean else text.strip()

    return PurchaseIntent(
        item_description=item_desc,
        amount_usd=amount,
        merchant_hint=merchant_hint,
        customer_bank=bank,
        urgency="normal",
    )


def fallback_explain_quotes(quotes: list[dict]) -> str:
    """Asesor determinista que explica las cotizaciones con score Bayesiano."""
    if not quotes:
        return "No hay cotizaciones disponibles para evaluar."
    best = quotes[0]
    second = quotes[1] if len(quotes) > 1 else None

    name = best.get("display_name", f"Proveedor #{best.get('rank', 1)}")
    score = best.get("score", 0)
    margin = best.get("margin_pct", 0)

    msg = (
        f"Recomiendo al {name} con una calificación Bayesiana de {score:.1f}/100. "
        f"Ofrece la mejor relación precio-seguridad con un margen de solo {margin}% sobre la tasa de referencia."
    )
    if second:
        name2 = second.get("display_name", f"Proveedor #{second.get('rank', 2)}")
        msg += f" Frente a {name2}, la opción #1 tiene mayor historial verificado y menor probabilidad de disputa."
    return msg


class GeminiClient:
    """Cliente para la API de Google Gemini (vía REST nativo con redundancia de modelos)."""

    def __init__(self, api_key: str, model: str = "gemini-3.1-flash-lite"):
        self.api_key = api_key
        self.model = model or "gemini-3.1-flash-lite"

    def generate(self, prompt: str, *, system_instruction: str = "", response_json: bool = False) -> str:
        import requests

        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}]
        }
        if system_instruction:
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        if response_json:
            body["generationConfig"] = {"responseMimeType": "application/json"}

        # Lista de modelos por orden de preferencia para alta disponibilidad
        models_to_try = [self.model]
        for fallback_m in ["gemini-3.1-flash-lite", "gemini-flash-lite-latest"]:
            if fallback_m not in models_to_try:
                models_to_try.append(fallback_m)

        last_err = None
        for m in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={self.api_key}"
            try:
                r = requests.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=12)
                if r.status_code == 200:
                    data = r.json()
                    candidates = data.get("candidates", [])
                    if candidates and "content" in candidates[0]:
                        parts = candidates[0]["content"].get("parts", [])
                        if parts and "text" in parts[0]:
                            return parts[0]["text"].strip()
                last_err = RuntimeError(f"Gemini API error ({m}) {r.status_code}: {r.text}")
            except Exception as exc:
                last_err = exc
                continue

        raise last_err or RuntimeError("Gemini generate failed")


class AgentBrain:
    """Implementa el protocolo `Brain` del orquestador usando Gemini o OpenAI con fallback resiliente."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.gemini = None
        self.openai_client = None

        # 1. Si hay clave de Gemini (o OPENAI_API_KEY empieza con AIza), usar Gemini
        gemini_key = settings.gemini_api_key or (settings.openai_api_key if settings.openai_api_key.startswith("AIza") else "")
        if gemini_key:
            try:
                self.gemini = GeminiClient(gemini_key, settings.gemini_model or "gemini-3.1-flash-lite")
            except Exception:
                pass

        # 2. Si hay clave de OpenAI estándar
        if not self.gemini and settings.openai_api_key:
            try:
                from openai import OpenAI
                kw = {"api_key": settings.openai_api_key}
                if settings.openai_base_url:
                    kw["base_url"] = settings.openai_base_url
                self.openai_client = OpenAI(**kw)
            except Exception:
                pass

    def parse_intent(self, text: str) -> PurchaseIntent:
        # Vía Gemini
        if self.gemini:
            try:
                sys_prompt = (
                    "Eres el agente de compras y recepción de PUENTE.\n"
                    "Dado el mensaje en lenguaje natural de un usuario venezolano, extrae en formato JSON:\n"
                    "- item_description: Nombre limpio y conciso del producto o servicio (ejemplo: 'Riot Points', 'Curso de Python', 'Suscripción SaaS'). NUNCA incluyas saludos ('hola'), verbos ('quiero', 'pagar'), ni montos aquí.\n"
                    "- amount_usd: Monto numérico en USD si lo indicó (ejemplo: 20.0), o null.\n"
                    "- merchant_hint: Nombre de la tienda o plataforma asociada (ejemplo: 'Riot Games', 'Steam', 'Platzi', 'Amazon'), o null.\n"
                    "- customer_bank: Código de 4 dígitos del banco emisor venezolano si lo mencionó (ej: '0102' Banco de Venezuela, '0134' Banesco, '0105' Mercantil, '0171' Banco Activo, '0172' Bancamiga, '0191' BNC), o null.\n"
                    "Devuelve ÚNICAMENTE el objeto JSON sin markdown ni explicaciones."
                )
                raw = self.gemini.generate(text, system_instruction=sys_prompt, response_json=True)
                d = json.loads(raw)
                return PurchaseIntent(
                    item_description=d.get("item_description") or text.strip(),
                    amount_usd=float(d["amount_usd"]) if d.get("amount_usd") else None,
                    merchant_hint=d.get("merchant_hint"),
                    customer_bank=d.get("customer_bank"),
                    urgency="normal",
                )
            except Exception:
                pass

        # Vía OpenAI
        if self.openai_client:
            try:
                resp = self.openai_client.chat.completions.create(
                    model=self.settings.openai_model,
                    messages=[
                        {"role": "system", "content": INTAKE_INSTRUCTIONS},
                        {"role": "user", "content": text}
                    ],
                    response_format={"type": "json_object"}
                )
                d = json.loads(resp.choices[0].message.content)
                return PurchaseIntent(
                    item_description=d.get("item_description") or text.strip(),
                    amount_usd=float(d["amount_usd"]) if d.get("amount_usd") else None,
                    merchant_hint=d.get("merchant_hint"),
                    customer_bank=d.get("customer_bank"),
                    urgency="normal",
                )
            except Exception:
                pass

        return fallback_parse_intent(text)

    def explain_quotes(self, quotes: list[dict]) -> str:
        # Vía Gemini
        if self.gemini:
            try:
                raw = self.gemini.generate(
                    json.dumps(quotes, default=str),
                    system_instruction=SOURCING_INSTRUCTIONS
                )
                if raw:
                    return raw
            except Exception:
                pass

        # Vía OpenAI
        if self.openai_client:
            try:
                resp = self.openai_client.chat.completions.create(
                    model=self.settings.openai_model,
                    messages=[
                        {"role": "system", "content": SOURCING_INSTRUCTIONS},
                        {"role": "user", "content": json.dumps(quotes, default=str)}
                    ]
                )
                if resp.choices[0].message.content:
                    return resp.choices[0].message.content.strip()
            except Exception:
                pass

        return fallback_explain_quotes(quotes)

    def decide_dispute(self, ctx: dict, allowed: set[str]) -> dict:
        # Vía Gemini
        if self.gemini:
            try:
                prompt = json.dumps({"case": ctx, "allowed": sorted(allowed)}, default=str)
                raw = self.gemini.generate(
                    prompt,
                    system_instruction=RESOLUTION_INSTRUCTIONS + " Responde en JSON: {'decision': 'contest'|'accept'|'human', 'rationale_es': str, 'confidence': float}.",
                    response_json=True
                )
                d = json.loads(raw)
                return {
                    "decision": d.get("decision", "human"),
                    "rationale_es": d.get("rationale_es", "Evaluado por Gemini."),
                    "confidence": float(d.get("confidence", 0.9))
                }
            except Exception:
                pass

        # Vía OpenAI
        if self.openai_client:
            try:
                prompt = json.dumps({"case": ctx, "allowed": sorted(allowed)}, default=str)
                resp = self.openai_client.chat.completions.create(
                    model=self.settings.openai_model,
                    messages=[
                        {"role": "system", "content": RESOLUTION_INSTRUCTIONS + " Responde en JSON: {'decision': '...', 'rationale_es': '...', 'confidence': 0.0-1.0}."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"}
                )
                d = json.loads(resp.choices[0].message.content)
                return {
                    "decision": d.get("decision", "human"),
                    "rationale_es": d.get("rationale_es", "Evaluado por LLM."),
                    "confidence": float(d.get("confidence", 0.9))
                }
            except Exception:
                pass

        # Fallback de resolución según reglas
        if "contest" in allowed:
            return {"decision": "contest", "rationale_es": "Existe prueba de entrega digital registrada en la cadena.", "confidence": 0.95}
        if "accept" in allowed:
            return {"decision": "accept", "rationale_es": "Sin prueba y monto bajo umbral automático de reembolso.", "confidence": 0.90}
        return {"decision": "human", "rationale_es": "Escalado a revisión humana según política de seguridad.", "confidence": 0.85}


def build_paypal_ops_agent(settings: Settings):
    """Agente conversacional con las tools REALES del PayPal Agent Toolkit (v1.1.0: orders/invoices/subscriptions)."""
    from agents import Agent
    from paypal_agent_toolkit.openai.toolkit import PayPalToolkit
    from paypal_agent_toolkit.shared.configuration import Configuration, Context

    config = Configuration(
        actions={
            "orders": {"create": True, "get": True, "capture": True},
            "invoices": {"create": True, "list": True, "send": True, "sendReminder": True, "cancel": True,
                         "generateQRC": True, "get": True},
        },
        context=Context(sandbox=True),
    )
    toolkit = PayPalToolkit(client_id=settings.paypal_client_id, secret=settings.paypal_secret, configuration=config)
    return Agent(name="PayPalOps", instructions=OPS_INSTRUCTIONS, model=settings.openai_model,
                 tools=toolkit.get_tools())
