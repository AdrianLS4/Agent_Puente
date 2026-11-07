// Backend simulado EN EL NAVEGADOR. Sirve para ver y ajustar la interfaz sin servidor ni API key.
// No es el agente real: el video debe grabarse contra el backend (npm run dev sin ?mock).
import { sleep } from "./lib";
import type {
  Api, ChatReply, Offer, OutboxMessage, Pending, RejectReason, ReportInput, RequestState, RequestView, Template,
} from "./types";

const ITEMS = [
  { id: 1, match: /python|curso/i, title: "Curso de Python (licencia anual)", usd: 29, merchant: "CursoOnline Demo" },
  { id: 2, match: /saas|suscrip/i, title: "Suscripción SaaS (1 año)", usd: 120, merchant: "CursoOnline Demo" },
  { id: 3, match: /teclado/i, title: "Teclado mecánico", usd: 45, merchant: "TiendaTech Demo" },
];
const BANKS: [RegExp, string][] = [
  [/banesco/i, "0134"], [/mercantil/i, "0105"], [/provincial/i, "0108"], [/venezuela|bdv/i, "0102"],
];
const RATE = 512.4;
const PROVIDERS = [
  { name: "Proveedor B (veterano)", margin: 2.5, done: 140, disp: 3, min: 12, bank: "0105", phone: "04140000002", doc: "V-00000002", holder: "Titular Demo B" },
  { name: "Proveedor A (nuevo, barato)", margin: 1.5, done: 3, disp: 0, min: 20, bank: "0102", phone: "04140000001", doc: "V-00000001", holder: "Titular Demo A" },
  { name: "Proveedor C (rápido, caro)", margin: 5.0, done: 60, disp: 0, min: 4, bank: "0108", phone: "04140000003", doc: "V-00000003", holder: "Titular Demo C" },
];

interface Session { itemId: number | null; bankCode: string | null; offers: Offer[]; pending: Pending | null }

export function createMockApi(): Api {
  let sessions: Record<string, Session> = {};
  let req: (RequestView & { inboxSeq: number }) | null = null;
  let providerIdx = 0;

  const now = () => new Date().toISOString();
  const addMsg = (role: OutboxMessage["role"], template: Template, body: string) => {
    if (!req) return;
    req.inboxSeq += 1;
    req.inbox.push({ id: req.inboxSeq, role, template, body, createdAt: now() });
  };
  const ev = (label: string) => req?.events.push({ at: now(), label });
  const setState = (s: RequestState, label: string, minutes: number | null) => {
    if (!req) return;
    req.state = s;
    req.deadline = minutes ? new Date(Date.now() + minutes * 60_000).toISOString() : null;
    ev(label);
  };
  const ves = (n: number) => n.toLocaleString("es-VE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function buildOffers(usd: number): Offer[] {
    const totals = PROVIDERS.map((p) => Math.round(usd * RATE * (1 + p.margin / 100) * 100) / 100);
    const best = Math.min(...totals);
    return PROVIDERS.map((p, i) => ({
      rank: i + 1, provider: p.name, totalVes: totals[i], rateVesPerUsd: Math.round(RATE * (1 + p.margin / 100) * 100) / 100,
      pctMoreThanCheapest: Math.round((totals[i] / best - 1) * 1000) / 10, completedOps: p.done, disputedOps: p.disp,
      avgMinutesToConfirm: p.min,
    }));
  }

  const empty = (reply: string, extra: Partial<ChatReply> = {}): ChatReply =>
    ({ reply, trace: [], offers: null, pending: null, requestId: req?.requestId ?? null, ...extra });

  return {
    simulated: true,

    async chat(sid, text) {
      await sleep(500);
      const s = (sessions[sid] ??= { itemId: null, bankCode: null, offers: [], pending: null });
      const item = ITEMS.find((i) => i.match.test(text));
      const bank = BANKS.find(([r]) => r.test(text));
      if (item) s.itemId = item.id;
      if (bank) s.bankCode = bank[1];
      if (!s.itemId) {
        return empty("No tengo ese producto en el catálogo. Por ahora puedo pagar: el curso de Python (US$ 29), la suscripción SaaS (US$ 120) y el teclado mecánico (US$ 45).",
          { trace: [{ name: "list_catalog", summary: "3 productos disponibles", ok: true }] });
      }
      if (!s.bankCode) return empty("Entendido. ¿Desde qué banco harías el Pago Móvil?");
      const it = ITEMS.find((i) => i.id === s.itemId)!;
      s.offers = buildOffers(it.usd);
      await sleep(400);
      return empty("Encontré tres proveedores verificados. El primero tiene más operaciones completadas y un precio dentro del rango; es el que recomiendo.", {
        trace: [
          { name: "get_quotes", summary: `${it.title}, banco ${s.bankCode}: 3 ofertas, 3 proveedores descartados`, ok: true },
          { name: "propose_purchase", summary: "oferta 1, pendiente de confirmación del usuario", ok: true },
        ],
        offers: s.offers,
        pending: (s.pending = { rank: 1, provider: s.offers[0].provider, totalVes: s.offers[0].totalVes, item: it.title, amountUsd: it.usd }),
      });
    },

    async confirm(sid) {
      await sleep(600);
      const s = sessions[sid];
      if (!s?.pending) return empty("No hay ninguna compra pendiente de confirmar.");
      const it = ITEMS.find((i) => i.id === s.itemId)!;
      const p = PROVIDERS[s.pending.rank - 1];
      providerIdx = s.pending.rank - 1;
      req = {
        requestId: 41, state: "AWAITING_PROVIDER_APPROVAL", item: it.title, amountUsd: it.usd, merchant: it.merchant,
        totalVes: s.pending.totalVes, providerName: p.name, lockMode: "approve",
        deadline: new Date(Date.now() + 15 * 60_000).toISOString(), approveUrl: "https://www.sandbox.paypal.com/checkoutnow?token=DEMO",
        payment: null, report: null, paypalOrderId: "5O190127TN364715T", inbox: [], inboxSeq: 0,
        events: [{ at: now(), label: "Solicitud creada y orden de PayPal generada" }], demoProviderToken: "tok-demo-provider-2",
      };
      addMsg("provider", "lock_request",
        `Compra #41: ${it.title} — US$ ${it.usd.toFixed(2)} a ${it.merchant}.\n1) Aprueba el pago en PayPal.\n   Aprobar no mueve tu dinero todavía: PUENTE solo cobra cuando tú confirmes que recibiste los Bs.\n2) Después le mostramos tus datos de Pago Móvil al cliente, que te pagará Bs. ${ves(s.pending.totalVes)}.\nTienes 15 min para aprobar.`);
      s.pending = null;
      return empty("Orden creada. Le avisé al proveedor para que bloquee el pago en PayPal. Cuando lo haga te muestro los datos para pagar.", {
        trace: [{ name: "confirm_purchase", summary: "ejecutado por el código tras el sí del usuario", ok: true }],
      });
    },

    async cancel(sid) {
      const s = sessions[sid];
      if (s) s.pending = null;
      return empty("Cancelado. No se movió nada.");
    },

    async view() {
      await sleep(80);
      if (!req) throw new Error("sin solicitud");
      return JSON.parse(JSON.stringify(req)) as RequestView;
    },

    async providerApprove() {
      await sleep(500);
      if (!req || req.state !== "AWAITING_PROVIDER_APPROVAL") return;
      const p = PROVIDERS[providerIdx];
      req.payment = { bank: p.bank, phone: p.phone, idDocument: p.doc, holder: p.holder, totalVes: req.totalVes };
      setState("AWAITING_VES", "El proveedor aprobó el pago en PayPal", 30);
      addMsg("customer", "pay_instructions", "El proveedor ya bloqueó tu pago en PayPal. Paga exactamente el monto indicado por Pago Móvil.");
      addMsg("provider", "await_customer", `Pago bloqueado. Esperando los Bs. ${ves(req.totalVes)} del cliente.`);
    },

    async report(_id, i: ReportInput) {
      await sleep(400);
      if (!req || req.state !== "AWAITING_VES") return;
      req.report = { reference: i.reference, amountVes: i.amountVes, payerBank: i.payerBank, payerPhone: i.payerPhone };
      setState("VES_REPORTED", "El cliente envió la referencia del pago", 20);
      addMsg("provider", "verify_payment", `El cliente dice que pagó Bs. ${ves(i.amountVes)}. Referencia ${i.reference}.`);
      addMsg("customer", "report_received", "Recibimos tu referencia. El proveedor la está verificando.");
    },

    async providerConfirm() {
      await sleep(500);
      if (!req || req.state !== "VES_REPORTED") return;
      setState("VES_CONFIRMED", "El proveedor confirmó que recibió los bolívares", null);
      addMsg("customer", "ves_confirmed", "El proveedor confirmó tu pago. Estamos completando la compra.");
      await sleep(900);
      setState("PAID", "Pago capturado en PayPal y enviado al comercio", null);
      addMsg("customer", "paid", `Pagamos US$ ${req.amountUsd.toFixed(2)} a ${req.merchant}. Ahora esperamos la entrega.`);
      addMsg("provider", "captured", `Se cobraron US$ ${req.amountUsd.toFixed(2)} de tu PayPal para ${req.merchant}.`);
      setState("AWAITING_DELIVERY", "Esperando la entrega del comercio", null);
    },

    async providerReject(_id, reason: RejectReason) {
      await sleep(400);
      if (!req || req.state !== "VES_REPORTED") return;
      if (reason === "wrong_reference") {
        setState("AWAITING_VES", "El proveedor no encontró la referencia", 30);
        req.report = null;
        addMsg("customer", "ref_mismatch", "El proveedor no encuentra esa referencia. Revisa el comprobante y envíala de nuevo.");
      } else {
        setState("EXPIRED", "El proveedor no recibió el pago completo", null);
        addMsg("customer", "expired", "El proveedor no confirmó tu pago. Cerramos la solicitud; si ya pagaste, escribe a soporte con tu comprobante.");
        addMsg("provider", "released", "Liberamos tu reserva en PayPal; no se cobró nada.");
      }
    },

    async providerSms() {
      return this.providerConfirm(0, "");
    },

    async reset() {
      sessions = {};
      req = null;
    },
  };
}
