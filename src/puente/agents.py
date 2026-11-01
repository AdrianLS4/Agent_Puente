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


class AgentBrain:
    """Implementa el protocolo `Brain` del orquestador usando el Agents SDK."""

    def __init__(self, settings: Settings):
        from agents import Agent  # import perezoso

        self.settings = settings
        self.intake = Agent(name="Intake", instructions=INTAKE_INSTRUCTIONS,
                            model=settings.openai_model, output_type=PurchaseIntent)
        self.sourcing = Agent(name="Sourcing", instructions=SOURCING_INSTRUCTIONS, model=settings.openai_model)
        self.resolution = Agent(name="Resolution", instructions=RESOLUTION_INSTRUCTIONS,
                                model=settings.openai_model, output_type=DisputeDecision)

    def parse_intent(self, text: str) -> PurchaseIntent:
        from agents import Runner
        return Runner.run_sync(self.intake, text).final_output

    def explain_quotes(self, quotes: list[dict]) -> str:
        from agents import Runner
        return str(Runner.run_sync(self.sourcing, json.dumps(quotes, default=str)).final_output)

    def decide_dispute(self, ctx: dict, allowed: set[str]) -> dict:
        from agents import Runner
        payload = json.dumps({"case": ctx, "allowed": sorted(allowed)})
        out: DisputeDecision = Runner.run_sync(self.resolution, payload).final_output
        return {"decision": out.decision, "rationale_es": out.rationale_es, "confidence": out.confidence}


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
