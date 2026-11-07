// Contrato entre la UI y el backend de PUENTE. Los nombres de estados y plantillas son los mismos que
// usa el orquestador (state_machine.py y orchestrator.notify), para que la UI no invente nada.

export type RequestState =
  | "QUOTED"
  | "AWAITING_PROVIDER_APPROVAL"
  | "AWAITING_VES"
  | "VES_REPORTED"
  | "VES_CONFIRMED"
  | "PAID"
  | "AWAITING_DELIVERY"
  | "DELIVERED"
  | "COMPLETED"
  | "DISPUTED"
  | "REFUND_VES_PENDING"
  | "REFUNDED"
  | "HUMAN_REVIEW"
  | "EXPIRED"
  | "FAILED";

export type Template =
  | "lock_request"
  | "await_customer"
  | "verify_payment"
  | "captured"
  | "released"
  | "return_ves"
  | "pay_instructions"
  | "report_received"
  | "ves_confirmed"
  | "ref_mismatch"
  | "paid"
  | "expired";

export type Role = "customer" | "provider" | "operator";

/** Fila de la tabla outbox: lo que el agente "envía" a una persona. */
export interface OutboxMessage {
  id: number;
  role: Role;
  template: Template;
  body: string;
  createdAt: string; // ISO
}

export interface Offer {
  rank: number;
  provider: string;
  totalVes: number;
  rateVesPerUsd: number;
  pctMoreThanCheapest: number;
  completedOps: number;
  disputedOps: number;
  avgMinutesToConfirm: number;
}

/** Compra propuesta por el modelo que espera el "sí" del usuario. Solo el código la ejecuta. */
export interface Pending {
  rank: number;
  provider: string;
  totalVes: number;
  item: string;
  amountUsd: number;
}

export interface ToolStep {
  name: string;
  summary: string;
  ok: boolean;
}

/** Respuesta de POST /chat y /chat/{sid}/confirm. */
export interface ChatReply {
  reply: string;
  trace: ToolStep[];
  offers: Offer[] | null;
  pending: Pending | null;
  requestId: number | null;
}

export interface PaymentInstructions {
  bank: string;
  phone: string;
  idDocument: string;
  holder: string;
  totalVes: number;
}

export interface VesReport {
  reference: string;
  amountVes: number;
  payerBank: string | null;
  payerPhone: string | null;
}

export interface TimelineEvent {
  at: string;
  label: string;
}

/** Respuesta de GET /p2p/requests/{id}/view (solo demo: reúne lo que ven los tres actores). */
export interface RequestView {
  requestId: number;
  state: RequestState;
  item: string;
  amountUsd: number;
  merchant: string;
  totalVes: number;
  providerName: string;
  lockMode: "approve" | "authorize";
  deadline: string | null; // ISO del plazo vigente (aprobación, pago en Bs o verificación)
  approveUrl: string | null;
  payment: PaymentInstructions | null; // null hasta que el proveedor bloquea el pago
  report: VesReport | null;
  paypalOrderId: string | null;
  inbox: OutboxMessage[];
  events: TimelineEvent[];
  demoProviderToken: string;
}

export type RejectReason = "not_received" | "wrong_amount" | "wrong_reference";

export interface ReportInput {
  reference: string;
  amountVes: number;
  payerBank: string;
  payerPhone: string;
}

/** Todo lo que la UI necesita del servidor. Hay dos implementaciones: HTTP real y simulada. */
export interface Api {
  readonly simulated: boolean;
  chat(sessionId: string, text: string): Promise<ChatReply>;
  confirm(sessionId: string): Promise<ChatReply>;
  cancel(sessionId: string): Promise<ChatReply>;
  view(requestId: number): Promise<RequestView>;
  providerApprove(requestId: number): Promise<void>;
  report(requestId: number, input: ReportInput): Promise<void>;
  providerConfirm(requestId: number, token: string): Promise<void>;
  providerReject(requestId: number, reason: RejectReason, token: string): Promise<void>;
  providerSms(requestId: number, text: string, token: string): Promise<void>;
  reset(): Promise<void>;
}

// ---- Lo que la UI guarda para pintar ---------------------------------------------------------------

export type Screen = "customer" | "provider" | "state";

export type FeedItem =
  | { id: string; kind: "user"; text: string }
  | { id: string; kind: "agent"; text: string }
  | { id: string; kind: "service"; template: Template; text: string; tone: "plain" | "good" | "warn" | "bad" }
  | { id: string; kind: "offers"; offers: Offer[] }
  | { id: string; kind: "confirm"; pending: Pending; resolved: "yes" | "no" | null }
  | { id: string; kind: "trace"; steps: ToolStep[] };

export interface Delivery {
  screen: Screen;
  item: FeedItem;
}
