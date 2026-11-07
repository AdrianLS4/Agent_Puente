import { useEffect, useRef, useState } from "react";
import { fmtUsd, fmtVes } from "../lib";
import { dockFor } from "../routing";
import type { Demo } from "../useDemo";
import type { RejectReason } from "../types";
import { Bubble, Button, Card, Panel, Row, Waiting } from "./ui";

const REASONS: { id: RejectReason; label: string }[] = [
  { id: "not_received", label: "No me llegó el pago" },
  { id: "wrong_amount", label: "El monto no coincide" },
  { id: "wrong_reference", label: "No encuentro la referencia" },
];

function Dock({ demo, simulated }: { demo: Demo; simulated: boolean }) {
  const { view } = demo;
  const [reason, setReason] = useState<RejectReason>("not_received");
  const [sms, setSms] = useState("");
  const mode = dockFor(view?.state ?? null).provider;
  if (!view || mode === "none") return null;
  switch (mode) {
    case "approve": return (
      <Card title="Aprobar el pago en PayPal" tone="accent">
        <dl>
          <Row k="Comercio" v={view.merchant} />
          <Row k="Se pagará" v={fmtUsd(view.amountUsd)} />
          <Row k="El cliente te paga" v={fmtVes(view.totalVes)} mono />
        </dl>
        <p className="mt-2 text-xs text-muted">Aprobar no mueve tu dinero. PUENTE cobra solo cuando confirmes que recibiste los bolívares.</p>
        <div className="mt-3 flex flex-wrap gap-2">
          {!simulated && view.approveUrl && (
            <a href={view.approveUrl} target="_blank" rel="noreferrer" className="rounded-md border border-line px-3.5 py-2 text-sm font-medium hover:bg-sunken">Abrir PayPal</a>
          )}
          <Button data-testid="provider-approve" disabled={demo.busy !== null} onClick={() => demo.providerApprove()}>
            {simulated ? "Aprobar en PayPal" : "Ya aprobé en PayPal"}
          </Button>
        </div>
      </Card>
    );
    case "wait_customer": return <Waiting until={view.deadline}>El cliente tiene tus datos y está pagando.</Waiting>;
    case "verify": return (
      <Card title="Verifica en tu banco" tone="warn">
        {view.report && (
          <dl>
            <Row k="Monto esperado" v={fmtVes(view.totalVes)} mono />
            <Row k="Referencia" v={view.report.reference} mono />
            <Row k="Banco de origen" v={view.report.payerBank || "sin dato"} />
            <Row k="Teléfono" v={view.report.payerPhone || "sin dato"} mono />
          </dl>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          <Button data-testid="provider-confirm" disabled={demo.busy !== null} onClick={() => demo.providerConfirm()}>Recibí el pago</Button>
        </div>
        <div className="mt-3 flex gap-2">
          <select value={reason} onChange={(e) => setReason(e.target.value as RejectReason)} className="min-w-0 flex-1 rounded-md border border-line bg-bg px-2 py-1.5 text-sm">
            {REASONS.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
          </select>
          <Button variant="danger" disabled={demo.busy !== null} onClick={() => demo.providerReject(reason)}>Rechazar</Button>
        </div>
        <div className="mt-3 flex gap-2">
          <input value={sms} onChange={(e) => setSms(e.target.value)} placeholder="Pega aquí el SMS del banco" className="min-w-0 flex-1 rounded-md border border-line bg-bg px-2.5 py-1.5 text-sm outline-none focus:border-accent" />
          <Button variant="ghost" disabled={!sms.trim() || demo.busy !== null} onClick={() => demo.providerSms(sms.trim())}>Verificar SMS</Button>
        </div>
      </Card>
    );
    case "done": return <Waiting>Operación cobrada. Gracias.</Waiting>;
    case "closed": return <Waiting>La solicitud está cerrada. No se cobró nada de tu PayPal.</Waiting>;
  }
}

export function ProviderPanel({ demo, simulated }: { demo: Demo; simulated: boolean }) {
  const end = useRef<HTMLDivElement>(null);
  const items = demo.feeds.provider;
  const key = demo.view?.state ?? "none";
  useEffect(() => { end.current?.scrollIntoView({ block: "end", behavior: "smooth" }); }, [items.length, key]);
  return (
    <Panel testId="panel-provider" title="Proveedor" subtitle={demo.view?.providerName ?? "Aún sin solicitud asignada"}>
      <div className="flex-1 space-y-3 overflow-y-auto p-4" data-testid="feed-provider">
        {items.length === 0 && <p className="pt-2 text-sm text-muted">Aquí llegan los avisos del agente al proveedor. Todavía no hay ninguno.</p>}
        {items.map((i) => i.kind === "service" && <Bubble key={i.id} who="service" tone={i.tone} label="Puente">{i.text}</Bubble>)}
        <Dock demo={demo} simulated={simulated} />
        <div ref={end} />
      </div>
    </Panel>
  );
}
