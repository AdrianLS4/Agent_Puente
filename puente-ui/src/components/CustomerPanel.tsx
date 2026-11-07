import { useEffect, useRef, useState } from "react";
import { bankLabel, fmtUsd, fmtVes } from "../lib";
import { dockFor } from "../routing";
import type { Demo } from "../useDemo";
import type { FeedItem, Offer, RequestView } from "../types";
import { Bubble, Button, Card, Field, Panel, Row, Waiting } from "./ui";

function OffersCard({ offers }: { offers: Offer[] }) {
  return (
    <div className="w-full space-y-2">
      {offers.map((o) => (
        <div key={o.rank} className={`rounded-lg border p-3 text-sm ${o.rank === 1 ? "border-accent/60 bg-accent-soft" : "border-line bg-surface"}`}>
          <div className="flex items-baseline justify-between gap-3">
            <span className="font-medium">{o.rank}. {o.provider}</span>
            <span className="font-mono">{fmtVes(o.totalVes)}</span>
          </div>
          <p className="mt-1 text-xs text-muted">
            {o.completedOps} operaciones, {o.disputedOps} con disputa, confirma en {o.avgMinutesToConfirm} min.
            {o.pctMoreThanCheapest > 0 ? ` ${o.pctMoreThanCheapest}% sobre la más barata.` : " La más barata."}
          </p>
        </div>
      ))}
    </div>
  );
}

function ConfirmCard({ item, demo }: { item: Extract<FeedItem, { kind: "confirm" }>; demo: Demo }) {
  const p = item.pending;
  return (
    <Card title="Confirmar compra" tone="accent">
      <dl>
        <Row k="Producto" v={p.item} />
        <Row k="Precio" v={fmtUsd(p.amountUsd)} />
        <Row k="Tú pagas" v={fmtVes(p.totalVes)} mono />
        <Row k="Proveedor" v={p.provider} />
      </dl>
      {item.resolved === null ? (
        <div className="mt-3 flex gap-2">
          <Button data-testid="confirm-yes" disabled={demo.busy !== null} onClick={() => demo.resolveConfirm("yes")}>Confirmar</Button>
          <Button variant="ghost" disabled={demo.busy !== null} onClick={() => demo.resolveConfirm("no")}>Cancelar</Button>
        </div>
      ) : (
        <p className="mt-3 text-xs text-muted">{item.resolved === "yes" ? "Confirmada." : "Cancelada."}</p>
      )}
    </Card>
  );
}

function PaymentCard({ view }: { view: RequestView }) {
  const p = view.payment!;
  return (
    <Card title="Paga por Pago Móvil" tone="accent">
      <dl>
        <Row k="Monto exacto" v={fmtVes(p.totalVes)} mono copy={p.totalVes.toFixed(2)} />
        <Row k="Banco" v={bankLabel(p.bank)} copy={p.bank} />
        <Row k="Teléfono" v={p.phone} mono copy={p.phone} />
        <Row k="Cédula o RIF" v={p.idDocument} mono copy={p.idDocument} />
        <Row k="Titular" v={p.holder} />
      </dl>
    </Card>
  );
}

function ReportForm({ view, demo }: { view: RequestView; demo: Demo }) {
  const [reference, setReference] = useState("");
  const [bank, setBank] = useState("");
  const [phone, setPhone] = useState("");
  const ok = reference.trim().length >= 4;
  return (
    <Card title="Ya pagué">
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
        <Field testId="ref" label="Referencia" value={reference} onChange={setReference} inputMode="numeric" placeholder="00123456" />
        <Field label="Tu banco" value={bank} onChange={setBank} placeholder="Banesco" />
        <Field label="Tu teléfono" value={phone} onChange={setPhone} inputMode="numeric" placeholder="0414..." />
      </div>
      <Button
        data-testid="report"
        className="mt-3"
        disabled={!ok || demo.busy !== null}
        onClick={() => demo.report({ reference: reference.trim(), amountVes: view.totalVes, payerBank: bank.trim(), payerPhone: phone.trim() })}
      >
        Enviar referencia
      </Button>
    </Card>
  );
}

function Dock({ demo }: { demo: Demo }) {
  const { view } = demo;
  const mode = dockFor(view?.state ?? null).customer;
  if (!view || mode === "none") return null;
  switch (mode) {
    case "wait_lock": return <Waiting until={view.deadline}>Esperando que el proveedor bloquee el pago en PayPal.</Waiting>;
    case "pay": return (<><PaymentCard view={view} /><ReportForm view={view} demo={demo} /></>);
    case "wait_verify": return <Waiting until={view.deadline}>El proveedor está verificando tu referencia.</Waiting>;
    case "wait_capture": return <Waiting>Completando la compra.</Waiting>;
    case "done": return (
      <Waiting>
        {view.state === "DELIVERED" || view.state === "COMPLETED"
          ? `Compra completada y entrega activada (${view.item}).`
          : "Compra pagada. Falta la entrega del comercio."}
      </Waiting>
    );
    case "closed": return <Waiting>La solicitud está cerrada.</Waiting>;
  }
}

function Feed({ items, demo }: { items: FeedItem[]; demo: Demo }) {
  return items.map((i) => {
    switch (i.kind) {
      case "user": return <Bubble key={i.id} who="user">{i.text}</Bubble>;
      case "agent": return <Bubble key={i.id} who="agent" label="Puente">{i.text}</Bubble>;
      case "service": return <Bubble key={i.id} who="service" tone={i.tone} label="Puente">{i.text}</Bubble>;
      case "offers": return <OffersCard key={i.id} offers={i.offers} />;
      case "confirm": return <ConfirmCard key={i.id} item={i} demo={demo} />;
      case "trace": return null;
    }
  });
}

const SUGGESTIONS = ["Quiero el curso de Python, pago con Banesco", "Necesito pagar un teclado, tengo Mercantil"];

export function CustomerPanel({ demo }: { demo: Demo }) {
  const [text, setText] = useState("");
  const end = useRef<HTMLDivElement>(null);
  const items = demo.feeds.customer;
  const dockKey = demo.view?.state ?? "none";
  useEffect(() => { end.current?.scrollIntoView({ block: "end", behavior: "smooth" }); }, [items.length, dockKey]);

  const submit = (t: string) => {
    const v = t.trim();
    if (!v || demo.busy) return;
    setText("");
    void demo.send(v);
  };

  return (
    <Panel
      testId="panel-customer"
      title="Cliente"
      subtitle="Paga en bolívares, el comercio cobra en dólares"
      footer={
        <form className="flex gap-2 p-3" onSubmit={(e) => { e.preventDefault(); submit(text); }}>
          <input
            data-testid="chat-input"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Escribe lo que necesitas pagar"
            className="min-w-0 flex-1 rounded-md border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
          />
          <Button type="submit" data-testid="chat-send" disabled={!text.trim() || demo.busy !== null}>Enviar</Button>
        </form>
      }
    >
      <div className="flex-1 space-y-3 overflow-y-auto p-4" data-testid="feed-customer">
        {items.length === 0 && (
          <div className="space-y-3 pt-2">
            <p className="text-sm text-muted">Cuéntame qué quieres pagar y con qué banco. El agente busca proveedores y te propone el mejor.</p>
            <div className="flex flex-wrap gap-2">
              {SUGGESTIONS.map((s) => (
                <button key={s} type="button" onClick={() => submit(s)} className="rounded-full border border-line px-3 py-1.5 text-xs text-ink hover:bg-sunken">{s}</button>
              ))}
            </div>
          </div>
        )}
        <Feed items={items} demo={demo} />
        {demo.busy === "customer" && <p className="px-1 text-xs text-muted">Puente está trabajando</p>}
        <Dock demo={demo} />
        <div ref={end} />
      </div>
    </Panel>
  );
}
