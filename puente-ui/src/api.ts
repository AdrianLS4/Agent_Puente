// Cliente HTTP real. Cada método corresponde a un endpoint de server.py (los marcados NUEVO hay que agregarlos).
import type { Api, ChatReply, RejectReason, ReportInput, RequestView } from "./types";

const BASE = "/api";

async function http<T>(path: string, init?: RequestInit & { token?: string }): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (init?.token) headers["X-Provider-Token"] = init.token;
  const res = await fetch(BASE + path, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail?.message_es ?? j.detail ?? detail;
    } catch { /* cuerpo vacío */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return (await res.json()) as T;
}

const post = <T>(path: string, body?: unknown, token?: string) =>
  http<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body), token });

export const realApi: Api = {
  simulated: false,
  chat: (sessionId, text) => post<ChatReply>("/chat", { session_id: sessionId, text }),                 // NUEVO
  confirm: (sessionId) => post<ChatReply>(`/chat/${sessionId}/confirm`),                                 // NUEVO
  cancel: (sessionId) => post<ChatReply>(`/chat/${sessionId}/cancel`),                                   // NUEVO
  view: (id) => http<RequestView>(`/p2p/requests/${id}/view`),                                           // NUEVO
  providerApprove: async (id) => { await post(`/demo/provider/approve/${id}`); },                        // NUEVO (solo demo)
  report: async (id, i: ReportInput) => {
    await post(`/p2p/requests/${id}/report`, {
      bank_reference: i.reference, amount_ves: i.amountVes, payer_bank: i.payerBank, payer_phone: i.payerPhone,
    });
  },
  providerConfirm: async (id, token) => { await post(`/p2p/requests/${id}/provider/confirm`, undefined, token); },
  providerReject: async (id, reason: RejectReason, token) => {
    await post(`/p2p/requests/${id}/provider/reject`, { reason }, token);
  },
  providerSms: async (id, text, token) => { await post(`/p2p/requests/${id}/provider/sms`, { text }, token); },
  reset: async () => { await post("/demo/reset"); },                                                      // NUEVO (solo demo)
};
