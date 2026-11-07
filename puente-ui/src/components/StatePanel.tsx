import { fmtTime, fmtUsd, fmtVes } from "../lib";
import { STAGES, isClosed, stageOf } from "../routing";
import type { Demo } from "../useDemo";
import { Card, Countdown, Panel, Row } from "./ui";

function Stepper({ state }: { state: Demo["view"] extends infer V ? (V extends { state: infer S } ? S : never) | null : never }) {
  const at = stageOf(state);
  const closed = isClosed(state);
  return (
    <ol className="space-y-2.5">
      {STAGES.map((label, i) => {
        const done = !closed && at > i;
        const current = !closed && at === i;
        return (
          <li key={label} className="flex items-center gap-3 text-sm">
            <span
              className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[11px] ${
                done ? "border-accent bg-accent text-accent-ink" : current ? "border-accent text-accent" : "border-line text-muted"
              }`}
            >
              {done ? "✓" : i + 1}
            </span>
            <span className={done || current ? "text-ink" : "text-muted"}>{label}</span>
          </li>
        );
      })}
      {closed && <li className="pt-1 text-sm text-bad">Solicitud cerrada sin completarse ({state})</li>}
    </ol>
  );
}

export function StatePanel({ demo, simulated }: { demo: Demo; simulated: boolean }) {
  const { view } = demo;
  const steps = demo.feeds.state.flatMap((i) => (i.kind === "trace" ? i.steps : []));
  return (
    <Panel testId="panel-state" title="Estado de la operación" subtitle={view ? `Solicitud #${view.requestId}` : "Sin solicitud"}>
      <div className="flex-1 space-y-4 overflow-y-auto p-4" data-testid="feed-state">
        <Card title="Etapas">
          <Stepper state={view?.state ?? null} />
          {view?.deadline && <p className="mt-3 border-t border-line pt-2"><Countdown until={view.deadline} label="Plazo vigente" /></p>}
        </Card>

        {view && (
          <Card title="Resumen">
            <dl>
              <Row k="Producto" v={view.item} />
              <Row k="Comercio" v={view.merchant} />
              <Row k="Monto en dólares" v={fmtUsd(view.amountUsd)} />
              <Row k="Monto en bolívares" v={fmtVes(view.totalVes)} mono />
              <Row k="Proveedor" v={view.providerName} />
              <Row k="Reserva en PayPal" v={view.lockMode === "authorize" ? "retención real" : "aprobación sin cobro"} />
              <Row k="Orden PayPal" v={view.paypalOrderId ?? "sin crear"} mono />
            </dl>
          </Card>
        )}

        <Card title="Decisiones del agente">
          {steps.length === 0 ? (
            <p className="text-sm text-muted">Cuando el modelo llame a una herramienta, aparece aquí.</p>
          ) : (
            <ul className="space-y-2" data-testid="trace">
              {steps.map((s, i) => (
                <li key={i} className="text-sm">
                  <span className={`font-mono text-xs ${s.ok ? "text-accent" : "text-bad"}`}>{s.name}</span>
                  <p className="text-muted">{s.summary}</p>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-3 border-t border-line pt-2 text-xs text-muted">
            El modelo propone y explica. Mover dinero y cambiar de estado lo hace el código, y los datos de Pago Móvil nunca se envían al modelo.
          </p>
        </Card>

        {view && view.events.length > 0 && (
          <Card title="Historial">
            <ul className="space-y-1.5">
              {view.events.map((e, i) => (
                <li key={i} className="flex gap-3 text-sm">
                  <span className="shrink-0 font-mono text-xs text-muted">{fmtTime(e.at)}</span>
                  <span>{e.label}</span>
                </li>
              ))}
            </ul>
          </Card>
        )}

        {simulated && <p className="text-xs text-muted">Modo simulado: las respuestas no vienen del agente real.</p>}
      </div>
    </Panel>
  );
}
