import { useEffect, useState, type ButtonHTMLAttributes, type ReactNode } from "react";

export function Panel(props: { title: string; subtitle?: string; children: ReactNode; footer?: ReactNode; testId?: string }) {
  return (
    <section data-testid={props.testId} className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-line bg-surface">
      <header className="border-b border-line px-4 py-3">
        <h2 className="text-sm font-semibold tracking-tight">{props.title}</h2>
        {props.subtitle && <p className="mt-0.5 truncate text-xs text-muted">{props.subtitle}</p>}
      </header>
      <div className="flex min-h-0 flex-1 flex-col">{props.children}</div>
      {props.footer && <footer className="border-t border-line">{props.footer}</footer>}
    </section>
  );
}

type Tone = "plain" | "good" | "warn" | "bad";
const TONE: Record<Tone, string> = {
  plain: "border-line",
  good: "border-ok/50",
  warn: "border-warn/50",
  bad: "border-bad/50",
};

export function Bubble(props: { who: "user" | "agent" | "service"; tone?: Tone; label?: string; children: ReactNode }) {
  if (props.who === "user") {
    return (
      <div className="flex justify-end">
        <p className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-accent px-3.5 py-2 text-sm text-accent-ink">{props.children}</p>
      </div>
    );
  }
  const base = props.who === "agent" ? "bg-sunken border-transparent" : `bg-surface ${TONE[props.tone ?? "plain"]}`;
  return (
    <div className="flex flex-col items-start gap-1">
      {props.label && <span className="px-1 text-[11px] uppercase tracking-wide text-muted">{props.label}</span>}
      <div className={`max-w-[92%] whitespace-pre-wrap rounded-2xl rounded-bl-sm border px-3.5 py-2 text-sm leading-relaxed ${base}`}>
        {props.children}
      </div>
    </div>
  );
}

export function Card(props: { title?: string; children: ReactNode; tone?: "plain" | "accent" | "warn" }) {
  const tone = props.tone === "accent" ? "border-accent/40 bg-accent-soft" : props.tone === "warn" ? "border-warn/40 bg-warn-soft" : "border-line bg-surface";
  return (
    <div className={`rounded-lg border p-3.5 ${tone}`}>
      {props.title && <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">{props.title}</h3>}
      {props.children}
    </div>
  );
}

export function Button({ variant = "primary", className = "", ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" | "danger" }) {
  const v = {
    primary: "bg-accent text-accent-ink hover:opacity-90",
    ghost: "border border-line bg-surface text-ink hover:bg-sunken",
    danger: "border border-bad/50 bg-surface text-bad hover:bg-bad-soft",
  }[variant];
  return (
    <button
      {...rest}
      className={`rounded-md px-3.5 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${v} ${className}`}
    />
  );
}

export function Row(props: { k: string; v: ReactNode; mono?: boolean; copy?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-baseline justify-between gap-3 py-1 text-sm">
      <dt className="shrink-0 text-muted">{props.k}</dt>
      <dd className={`flex min-w-0 items-center gap-2 text-right ${props.mono ? "font-mono" : ""}`}>
        <span className="truncate">{props.v}</span>
        {props.copy && (
          <button
            type="button"
            className="shrink-0 rounded border border-line px-1.5 text-[11px] text-muted hover:bg-sunken"
            onClick={() => {
              void navigator.clipboard?.writeText(props.copy!).catch(() => undefined);
              setCopied(true);
              setTimeout(() => setCopied(false), 1200);
            }}
          >
            {copied ? "Copiado" : "Copiar"}
          </button>
        )}
      </dd>
    </div>
  );
}

export function Countdown({ until, label }: { until: string | null; label?: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  if (!until) return null;
  const s = Math.max(0, Math.round((new Date(until).getTime() - now) / 1000));
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return (
    <span className="font-mono text-xs text-muted">
      {label ? `${label} ` : ""}{mm}:{ss}
    </span>
  );
}

export function Field(props: { label: string; value: string; onChange: (v: string) => void; placeholder?: string; inputMode?: "numeric" | "decimal" | "text"; testId?: string }) {
  return (
    <label className="block text-xs text-muted">
      {props.label}
      <input
        data-testid={props.testId}
        value={props.value}
        inputMode={props.inputMode}
        placeholder={props.placeholder}
        onChange={(e) => props.onChange(e.target.value)}
        className="mt-1 w-full rounded-md border border-line bg-bg px-2.5 py-1.5 text-sm text-ink outline-none focus:border-accent"
      />
    </label>
  );
}

export function Waiting({ children, until }: { children: ReactNode; until?: string | null }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-line px-3.5 py-3 text-sm text-muted">
      <span>{children}</span>
      {until !== undefined && <Countdown until={until} />}
    </div>
  );
}
