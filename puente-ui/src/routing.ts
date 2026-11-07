// Reglas de "qué pantalla recibe qué". Es el único lugar donde se decide el destino de un mensaje.
// Se lee como una simulación de chat: tres pantallas, y cada cosa que ocurre entra por una de estas tres puertas:
//   1. La respuesta del agente al cliente (chat)            -> routeChatReply
//   2. Un mensaje de la outbox (el agente le escribe a X)   -> routeOutbox
//   3. El estado de la solicitud (qué puede hacer cada uno) -> dockFor / stageOf

import type { ChatReply, Delivery, OutboxMessage, RequestState, Template } from "./types";

// 1. Chat ---------------------------------------------------------------------------------------------
// Texto, ofertas y confirmación van al cliente. El rastro de herramientas va a Estado: el cliente no necesita
// ver "get_quotes(...)" pero un juez sí quiere ver que el modelo decidió llamarla.
export function routeChatReply(reply: ChatReply, idBase: string): Delivery[] {
  const out: Delivery[] = [];
  if (reply.trace.length) out.push({ screen: "state", item: { id: `${idBase}-t`, kind: "trace", steps: reply.trace } });
  if (reply.offers?.length) out.push({ screen: "customer", item: { id: `${idBase}-o`, kind: "offers", offers: reply.offers } });
  if (reply.reply.trim()) out.push({ screen: "customer", item: { id: `${idBase}-a`, kind: "agent", text: reply.reply } });
  if (reply.pending) out.push({ screen: "customer", item: { id: `${idBase}-c`, kind: "confirm", pending: reply.pending, resolved: null } });
  return out;
}

// 2. Outbox -------------------------------------------------------------------------------------------
type Tone = "plain" | "good" | "warn" | "bad";
const OUTBOX: Record<Template, { screen: "customer" | "provider"; tone: Tone }> = {
  // al proveedor
  lock_request:     { screen: "provider", tone: "plain" },
  await_customer:   { screen: "provider", tone: "plain" },
  verify_payment:   { screen: "provider", tone: "warn" },
  captured:         { screen: "provider", tone: "good" },
  released:         { screen: "provider", tone: "plain" },
  return_ves:       { screen: "provider", tone: "warn" },
  // al cliente
  pay_instructions: { screen: "customer", tone: "plain" },
  report_received:  { screen: "customer", tone: "plain" },
  ves_confirmed:    { screen: "customer", tone: "plain" },
  ref_mismatch:     { screen: "customer", tone: "bad" },
  paid:             { screen: "customer", tone: "good" },
  expired:          { screen: "customer", tone: "bad" },
};

// Plantillas que ya se muestran como tarjeta en la zona de acción; el texto plano se omite para no duplicarlo.
const SHOWN_AS_CARD: Template[] = ["pay_instructions", "verify_payment"];

export function routeOutbox(m: OutboxMessage): Delivery | null {
  if (m.role === "operator") return null; // el operador no tiene pantalla propia en la prueba
  const rule = OUTBOX[m.template];
  if (!rule || rule.screen !== m.role) return null; // plantilla desconocida o a otro destinatario: no se pinta
  if (SHOWN_AS_CARD.includes(m.template)) return null;
  return { screen: rule.screen, item: { id: `ob-${m.id}`, kind: "service", template: m.template, text: m.body, tone: rule.tone } };
}

// 3. Estado -> zona de acción ---------------------------------------------------------------------------
export type CustomerDock = "none" | "wait_lock" | "pay" | "wait_verify" | "wait_capture" | "done" | "closed";
export type ProviderDock = "none" | "approve" | "wait_customer" | "verify" | "done" | "closed";

const CUSTOMER: Partial<Record<RequestState, CustomerDock>> = {
  QUOTED: "none",
  AWAITING_PROVIDER_APPROVAL: "wait_lock",
  AWAITING_VES: "pay",
  VES_REPORTED: "wait_verify",
  VES_CONFIRMED: "wait_capture",
  PAID: "done",
  AWAITING_DELIVERY: "done",
  DELIVERED: "done",
  COMPLETED: "done",
  EXPIRED: "closed",
  FAILED: "closed",
  REFUNDED: "closed",
  REFUND_VES_PENDING: "closed",
  DISPUTED: "closed",
  HUMAN_REVIEW: "closed",
};

const PROVIDER: Partial<Record<RequestState, ProviderDock>> = {
  QUOTED: "none",
  AWAITING_PROVIDER_APPROVAL: "approve",
  AWAITING_VES: "wait_customer",
  VES_REPORTED: "verify",
  VES_CONFIRMED: "done",
  PAID: "done",
  AWAITING_DELIVERY: "done",
  DELIVERED: "done",
  COMPLETED: "done",
  EXPIRED: "closed",
  FAILED: "closed",
  REFUNDED: "closed",
  REFUND_VES_PENDING: "closed",
  DISPUTED: "closed",
  HUMAN_REVIEW: "closed",
};

export function dockFor(state: RequestState | null): { customer: CustomerDock; provider: ProviderDock } {
  if (!state) return { customer: "none", provider: "none" };
  return { customer: CUSTOMER[state] ?? "none", provider: PROVIDER[state] ?? "none" };
}

// Barra de etapas de la pantalla Estado.
export const STAGES = [
  "Solicitud",
  "Proveedor bloquea el pago",
  "Cliente paga en bolívares",
  "Proveedor verifica",
  "Cobro en PayPal",
  "Entrega",
] as const;

export function stageOf(state: RequestState | null): number {
  switch (state) {
    case null:
    case "QUOTED": return 0;
    case "AWAITING_PROVIDER_APPROVAL": return 1;
    case "AWAITING_VES": return 2;
    case "VES_REPORTED": return 3;
    case "VES_CONFIRMED": return 4;
    case "PAID":
    case "AWAITING_DELIVERY": return 5;
    case "DELIVERED":
    case "COMPLETED": return 6;
    default: return -1; // cerrada sin completarse
  }
}

export function isClosed(state: RequestState | null): boolean {
  return stageOf(state) === -1;
}

export function isFinished(state: RequestState | null): boolean {
  return stageOf(state) === 6;
}
