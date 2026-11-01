-- PUENTE — esquema de base de datos (SQLite para el hackathon; portable a Postgres:
-- AUTOINCREMENT -> GENERATED ALWAYS AS IDENTITY, TEXT json -> JSONB, datetime('now') -> now()).
-- Regla de oro: el estado de dinero vive aquí, NUNCA en la memoria del LLM.

PRAGMA foreign_keys = ON;

-- ───────────── Actores ─────────────
CREATE TABLE IF NOT EXISTS customers (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  display_name  TEXT NOT NULL,
  email         TEXT,
  default_bank  TEXT,                       -- código de banco VE, ej. '0102'
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Proveedor = persona/negocio verificado que tiene saldo USD en PayPal y cobra en VES.
CREATE TABLE IF NOT EXISTS providers (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  display_name           TEXT NOT NULL,
  paypal_email           TEXT NOT NULL,     -- cuenta SANDBOX personal en el demo
  paypal_payer_id        TEXT,
  paypal_verified        INTEGER NOT NULL DEFAULT 0 CHECK (paypal_verified IN (0,1)),
  kyc_level              INTEGER NOT NULL DEFAULT 0 CHECK (kyc_level BETWEEN 0 AND 3),
  status                 TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','banned')),
  completed_orders       INTEGER NOT NULL DEFAULT 0,
  disputed_orders        INTEGER NOT NULL DEFAULT 0,
  failed_orders          INTEGER NOT NULL DEFAULT 0,
  avg_settle_minutes     REAL    NOT NULL DEFAULT 30,
  usd_capacity_available REAL    NOT NULL DEFAULT 0,
  margin_pct             REAL    NOT NULL DEFAULT 3.0,   -- sobre la tasa de referencia
  fee_usd                REAL    NOT NULL DEFAULT 0,     -- fee fijo por operación
  accepts_banks          TEXT    NOT NULL DEFAULT '[]',  -- json: ["0102","0105",...]
  last_active_at         TEXT,
  created_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Comercio real al que se le paga con PayPal (en el demo: cuenta SANDBOX business).
CREATE TABLE IF NOT EXISTS merchants (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  name               TEXT NOT NULL,
  paypal_merchant_id TEXT,
  category           TEXT,
  delivery_type      TEXT NOT NULL CHECK (delivery_type IN ('digital','physical')),
  created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS catalog_items (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant_id INTEGER NOT NULL REFERENCES merchants(id),
  title       TEXT NOT NULL,
  price_usd   REAL NOT NULL CHECK (price_usd > 0),
  active      INTEGER NOT NULL DEFAULT 1
);

-- ───────────── Tasa de cambio ─────────────
CREATE TABLE IF NOT EXISTS fx_rates (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  pair        TEXT NOT NULL DEFAULT 'USD/VES',
  rate        REAL NOT NULL CHECK (rate > 0),     -- VES por 1 USD
  fetched_at  TEXT NOT NULL,
  is_fallback INTEGER NOT NULL DEFAULT 0
);

-- ───────────── Solicitud de compra (máquina de estados) ─────────────
CREATE TABLE IF NOT EXISTS purchase_requests (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key    TEXT NOT NULL UNIQUE,         -- evita doble cobro si el cliente reintenta
  parent_request_id  INTEGER REFERENCES purchase_requests(id),  -- reabierta tras failover
  excluded_providers TEXT NOT NULL DEFAULT '[]',   -- json: proveedores ya fallidos en esta cadena
  customer_id        INTEGER NOT NULL REFERENCES customers(id),
  merchant_id        INTEGER NOT NULL REFERENCES merchants(id),
  item_id            INTEGER NOT NULL REFERENCES catalog_items(id),
  amount_usd         REAL NOT NULL CHECK (amount_usd > 0),
  customer_bank      TEXT,
  state              TEXT NOT NULL DEFAULT 'CREATED',
  selected_quote_id  INTEGER,
  state_deadline_at  TEXT,                         -- el scheduler revisa esto (timeouts)
  created_at         TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cotizaciones: el top-N del ranking, con explicación auditable.
CREATE TABLE IF NOT EXISTS quotes (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id           INTEGER NOT NULL REFERENCES purchase_requests(id),
  provider_id          INTEGER NOT NULL REFERENCES providers(id),
  ref_rate_ves_per_usd REAL NOT NULL,              -- tasa de referencia usada
  rate_ves_per_usd     REAL NOT NULL,              -- tasa con margen del proveedor
  margin_pct           REAL NOT NULL,
  fee_usd              REAL NOT NULL,
  total_ves            REAL NOT NULL,
  score                REAL NOT NULL,
  score_breakdown      TEXT NOT NULL,              -- json: price/reliability/speed/...
  rank                 INTEGER NOT NULL,
  expires_at           TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'offered'
                         CHECK (status IN ('offered','selected','expired','rejected'))
);

-- Pago en bolívares (en el demo es SIMULADO; en producción sería Pago Móvil/transferencia).
CREATE TABLE IF NOT EXISTS ves_payments (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id    INTEGER NOT NULL REFERENCES purchase_requests(id),
  quote_id      INTEGER NOT NULL REFERENCES quotes(id),
  method        TEXT NOT NULL DEFAULT 'pago_movil',
  reference     TEXT UNIQUE,                       -- referencia bancaria: UNIQUE anti-reuso
  amount_ves    REAL,
  status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','confirmed','rejected','refund_pending','refunded')),
  confirmed_at  TEXT,
  refunded_at   TEXT
);

-- ───────────── PayPal ─────────────
CREATE TABLE IF NOT EXISTS paypal_orders (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id           INTEGER NOT NULL REFERENCES purchase_requests(id),
  provider_id          INTEGER NOT NULL REFERENCES providers(id),
  kind                 TEXT NOT NULL DEFAULT 'order' CHECK (kind IN ('order','invoice')),
  paypal_order_id      TEXT UNIQUE,
  paypal_invoice_id    TEXT,
  invoice_number       TEXT,
  invoice_url          TEXT,
  status               TEXT NOT NULL,              -- CREATED/APPROVED/COMPLETED/REFUNDED...
  approve_url          TEXT,
  capture_id           TEXT,
  amount_usd           REAL NOT NULL,
  captured_amount_usd  REAL,
  created_at           TEXT NOT NULL DEFAULT (datetime('now')),
  captured_at          TEXT,
  raw_json             TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id      INTEGER NOT NULL REFERENCES purchase_requests(id),
  delivery_type   TEXT NOT NULL CHECK (delivery_type IN ('digital','physical')),
  carrier         TEXT,
  tracking_number TEXT,
  paypal_tracker  TEXT,
  proof_hash      TEXT,                            -- hash del comprobante digital (licencia, recibo)
  proof_url       TEXT,
  status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','shipped','delivered','failed')),
  deadline_at     TEXT,
  delivered_at    TEXT
);

-- ───────────── Disputas ─────────────
CREATE TABLE IF NOT EXISTS disputes (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id         INTEGER NOT NULL REFERENCES purchase_requests(id),
  paypal_dispute_id  TEXT UNIQUE,
  reason             TEXT,                          -- MERCHANDISE_OR_SERVICE_NOT_RECEIVED, etc.
  amount_usd         REAL,
  status             TEXT NOT NULL DEFAULT 'open',
  proof_strength     TEXT CHECK (proof_strength IN ('strong','weak','none')),
  decision           TEXT CHECK (decision IN ('contest','accept','human')),
  decided_by         TEXT,                          -- 'agent' | 'rule' | 'human'
  rationale_es       TEXT,
  outcome            TEXT,                          -- 'won' | 'lost' | 'refunded'
  respond_by         TEXT,
  opened_at          TEXT NOT NULL DEFAULT (datetime('now')),
  closed_at          TEXT
);

CREATE TABLE IF NOT EXISTS dispute_evidence (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  dispute_id  INTEGER NOT NULL REFERENCES disputes(id),
  kind        TEXT NOT NULL,                        -- PROOF_OF_FULFILLMENT, tracking, chat_log...
  content     TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ───────────── Auditoría y operaciones ─────────────
-- Log append-only: cada cambio de estado y cada acción del agente queda aquí.
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id  INTEGER REFERENCES purchase_requests(id),
  ts          TEXT NOT NULL DEFAULT (datetime('now')),
  actor       TEXT NOT NULL CHECK (actor IN ('system','agent','human','paypal_webhook','customer','provider')),
  event_type  TEXT NOT NULL,
  from_state  TEXT,
  to_state    TEXT,
  payload     TEXT                                  -- json
);

CREATE TABLE IF NOT EXISTS error_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id  INTEGER REFERENCES purchase_requests(id),
  code        TEXT NOT NULL,                        -- ver src/puente/errors.py
  action      TEXT NOT NULL,                        -- qué decidió el catálogo
  attempt     INTEGER NOT NULL DEFAULT 0,
  detail      TEXT,                                 -- json
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Idempotencia de webhooks de PayPal (PayPal puede reenviar el mismo evento).
CREATE TABLE IF NOT EXISTS webhook_events (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  paypal_event_id  TEXT NOT NULL UNIQUE,
  event_type       TEXT NOT NULL,
  payload          TEXT NOT NULL,
  processed_at     TEXT
);

-- Cola para decisiones que el agente NO puede tomar solo.
CREATE TABLE IF NOT EXISTS human_review_queue (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id  INTEGER NOT NULL REFERENCES purchase_requests(id),
  reason_code TEXT NOT NULL,
  note        TEXT,
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_req_state_deadline ON purchase_requests(state, state_deadline_at);
CREATE INDEX IF NOT EXISTS idx_quotes_request      ON quotes(request_id, rank);
CREATE INDEX IF NOT EXISTS idx_events_request      ON events(request_id, ts);
CREATE INDEX IF NOT EXISTS idx_errors_code         ON error_events(code);
CREATE INDEX IF NOT EXISTS idx_providers_active    ON providers(status, kyc_level);
