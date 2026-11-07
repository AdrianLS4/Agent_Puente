import { useMemo, useState } from "react";
import { realApi } from "./api";
import { CustomerPanel } from "./components/CustomerPanel";
import { ProviderPanel } from "./components/ProviderPanel";
import { StatePanel } from "./components/StatePanel";
import { Button } from "./components/ui";
import { createMockApi } from "./mock";
import type { Screen } from "./types";
import { useDemo } from "./useDemo";

// ?mock en la URL o VITE_MOCK=1 usan el servidor simulado del navegador.
const useMock = new URLSearchParams(location.search).has("mock") || import.meta.env.VITE_MOCK === "1";

const TABS: { id: Screen; label: string }[] = [
  { id: "customer", label: "Cliente" },
  { id: "provider", label: "Proveedor" },
  { id: "state", label: "Estado" },
];

export default function App() {
  const api = useMemo(() => (useMock ? createMockApi() : realApi), []);
  const demo = useDemo(api);
  const [tab, setTab] = useState<Screen>("customer");
  const show = (s: Screen) => (tab === s ? "flex" : "hidden") + " min-h-0 lg:flex";

  return (
    <div className="mx-auto flex h-dvh max-w-[1400px] flex-col gap-3 p-3 sm:p-4">
      <header className="flex items-center justify-between gap-3">
        <div className="flex items-baseline gap-3">
          <h1 className="text-base font-semibold tracking-tight">Puente</h1>
          {api.simulated && <span className="rounded border border-warn/50 bg-warn-soft px-1.5 py-0.5 text-[11px] text-warn">Simulado</span>}
        </div>
        <Button variant="ghost" data-testid="reset" disabled={demo.busy !== null} onClick={() => demo.reset()}>Reiniciar</Button>
      </header>

      {demo.error && (
        <div role="alert" className="flex items-center justify-between gap-3 rounded-md border border-bad/50 bg-bad-soft px-3 py-2 text-sm text-bad">
          <span>{demo.error}</span>
          <button type="button" onClick={demo.dismissError} className="text-xs underline">Cerrar</button>
        </div>
      )}

      <nav className="grid grid-cols-3 gap-1 rounded-lg border border-line bg-surface p-1 lg:hidden">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={`rounded-md px-2 py-1.5 text-sm ${tab === t.id ? "bg-accent text-accent-ink" : "text-muted"}`}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <main className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)_minmax(0,0.85fr)]">
        <div className={show("customer")}><div className="flex min-h-0 w-full flex-col [&>section]:flex-1"><CustomerPanel demo={demo} /></div></div>
        <div className={show("provider")}><div className="flex min-h-0 w-full flex-col [&>section]:flex-1"><ProviderPanel demo={demo} simulated={api.simulated} /></div></div>
        <div className={show("state")}><div className="flex min-h-0 w-full flex-col [&>section]:flex-1"><StatePanel demo={demo} simulated={api.simulated} /></div></div>
      </main>
    </div>
  );
}
