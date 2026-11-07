export const fmtVes = (n: number) =>
  "Bs. " + n.toLocaleString("es-VE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const fmtUsd = (n: number) => "US$ " + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const fmtTime = (iso: string) =>
  new Date(iso).toLocaleTimeString("es-VE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

export const BANK_NAMES: Record<string, string> = {
  "0102": "Banco de Venezuela",
  "0105": "Mercantil",
  "0108": "Provincial",
  "0134": "Banesco",
};

export const bankLabel = (code: string) => (BANK_NAMES[code] ? `${BANK_NAMES[code]} (${code})` : code);

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
