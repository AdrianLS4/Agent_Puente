import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { routeChatReply, routeOutbox } from "./routing";
import type { Api, ChatReply, Delivery, FeedItem, RejectReason, ReportInput, RequestView, Screen } from "./types";

type Feeds = Record<Screen, FeedItem[]>;
const EMPTY: Feeds = { customer: [], provider: [], state: [] };

/** Estado de toda la prueba. Las tres pantallas leen de aquí; nada más habla con el servidor. */
export function useDemo(api: Api) {
  const [feeds, setFeeds] = useState<Feeds>(EMPTY);
  const [view, setView] = useState<RequestView | null>(null);
  const [busy, setBusy] = useState<Screen | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState(() => crypto.randomUUID());
  const [requestId, setRequestId] = useState<number | null>(null);
  const seen = useRef(new Set<number>());
  const seq = useRef(0);
  const nextId = (p: string) => `${p}${(seq.current += 1)}`;

  const deliver = useCallback((ds: Delivery[]) => {
    if (!ds.length) return;
    setFeeds((f) => {
      const n = { ...f };
      for (const d of ds) n[d.screen] = [...n[d.screen], d.item];
      return n;
    });
  }, []);

  // Cada acción pasa por aquí: marca la pantalla ocupada, captura el error y lo muestra sin romper la prueba.
  const run = useCallback(async (screen: Screen, fn: () => Promise<void>) => {
    setBusy(screen);
    setError(null);
    try { await fn(); }
    catch (e) { setError(e instanceof Error ? e.message : "Error desconocido"); }
    finally { setBusy(null); }
  }, []);

  const handleReply = useCallback((r: ChatReply) => {
    deliver(routeChatReply(r, nextId("r")));
    if (r.requestId != null) {
      setRequestId(r.requestId);
    } else if (r.pending != null) {
      setRequestId(null);
      setView(null);
    }
  }, [deliver]);

  const send = useCallback((text: string) => run("customer", async () => {
    deliver([{ screen: "customer", item: { id: nextId("u"), kind: "user", text } }]);
    handleReply(await api.chat(sessionId, text));
  }), [api, deliver, handleReply, run, sessionId]);

  const resolveConfirm = useCallback((answer: "yes" | "no") => run("customer", async () => {
    setFeeds((f) => ({
      ...f,
      customer: f.customer.map((i) => (i.kind === "confirm" && i.resolved === null ? { ...i, resolved: answer } : i)),
    }));
    handleReply(await (answer === "yes" ? api.confirm(sessionId) : api.cancel(sessionId)));
  }), [api, handleReply, run, sessionId]);

  // Sondeo del estado de la solicitud; los mensajes nuevos de la outbox se reparten con las reglas de routing.ts.
  useEffect(() => {
    if (requestId == null) return;
    let alive = true;
    const tick = async () => {
      try {
        const v = await api.view(requestId);
        if (!alive) return;
        setView(v);
        const fresh = v.inbox.filter((m) => !seen.current.has(m.id));
        fresh.forEach((m) => seen.current.add(m.id));
        deliver(fresh.map(routeOutbox).filter((d): d is Delivery => d !== null));
      } catch { /* el siguiente ciclo reintenta */ }
    };
    void tick();
    const t = setInterval(tick, 1200);
    return () => { alive = false; clearInterval(t); };
  }, [api, requestId, deliver]);

  const providerApprove = useCallback(() => run("provider", () => api.providerApprove(requestId!)), [api, requestId, run]);
  const report = useCallback((i: ReportInput) => run("customer", () => api.report(requestId!, i)), [api, requestId, run]);
  const providerConfirm = useCallback(
    () => run("provider", () => api.providerConfirm(requestId!, view!.demoProviderToken)), [api, requestId, run, view]);
  const providerReject = useCallback(
    (reason: RejectReason) => run("provider", () => api.providerReject(requestId!, reason, view!.demoProviderToken)),
    [api, requestId, run, view]);
  const providerSms = useCallback(
    (text: string) => run("provider", () => api.providerSms(requestId!, text, view!.demoProviderToken)),
    [api, requestId, run, view]);

  const reset = useCallback(() => run("state", async () => {
    await api.reset();
    seen.current = new Set();
    setFeeds(EMPTY);
    setView(null);
    setRequestId(null);
    setSessionId(crypto.randomUUID());
  }), [api, run]);

  return useMemo(() => ({
    feeds, view, busy, error, send, resolveConfirm, providerApprove, report, providerConfirm, providerReject,
    providerSms, reset, dismissError: () => setError(null),
  }), [feeds, view, busy, error, send, resolveConfirm, providerApprove, report, providerConfirm, providerReject, providerSms, reset]);
}

export type Demo = ReturnType<typeof useDemo>;
