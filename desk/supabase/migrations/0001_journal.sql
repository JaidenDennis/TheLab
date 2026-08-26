-- Trading Desk — journal tables (spec §6)
-- Raw fills are immutable; trades are regenerable; everything else is Jay's input.

create extension if not exists pgcrypto;

-- Raw broker fills, exactly as imported. Never edited, never deleted.
create table fills (
  id            uuid primary key default gen_random_uuid(),
  account       text not null,
  order_id      text not null,
  exec_id       text not null,
  contract      text not null,             -- e.g. MNQU6
  product       text not null,             -- root, e.g. MNQ
  side          text not null check (side in ('buy', 'sell')),
  qty           integer not null check (qty > 0),
  price         numeric not null,
  fees          numeric not null default 0,
  filled_at     timestamptz not null,
  raw_json      jsonb not null,            -- the source CSV row, untouched
  imported_at   timestamptz not null default now(),
  unique (account, order_id, exec_id)      -- idempotent re-import
);

-- Reconstructed round-trips. Regenerable from fills at any time.
create table trades (
  id             uuid primary key default gen_random_uuid(),
  account        text not null,
  contract       text not null,
  product        text not null,
  direction      text not null check (direction in ('long', 'short')),
  entry_at       timestamptz not null,
  exit_at        timestamptz not null,
  avg_entry      numeric not null,
  avg_exit       numeric not null,
  size           integer not null,          -- peak absolute position
  qty_traded     integer not null,          -- total contracts entered
  gross_pnl      numeric not null,          -- points x point value
  fees           numeric not null,
  net_pnl        numeric not null,
  mae            numeric,                   -- nullable: needs market data
  mfe            numeric,
  fill_ids       uuid[] not null,
  session_date   date not null,             -- ET trading date, links to sessions
  checklist_id   uuid,                      -- set during enrichment
  narrative      text,
  rebuilt_at     timestamptz not null default now()
);
-- Stable identity across rebuilds: re-import upserts on this key so enrichment
-- (tags, narrative, attachments, note links) survives trade regeneration.
create unique index trades_natural_key on trades (account, contract, direction, entry_at);
create index trades_session_date_idx on trades (session_date);
create index trades_account_contract_idx on trades (account, contract);

-- One per trading day, created before the open.
create table sessions (
  id             uuid primary key default gen_random_uuid(),
  session_date   date not null unique,
  htf_bias       text not null check (htf_bias in ('bullish', 'bearish', 'neutral')),
  key_levels     text not null,
  hunting        text not null,
  invalidation   text not null,
  day_read_json  jsonb,                     -- frozen pre-open buddy scorecard
  created_at     timestamptz not null default now()
);

-- Declared rule set, version-stamped.
create table rule_versions (
  id          uuid primary key default gen_random_uuid(),
  version     integer not null unique,
  rules_json  jsonb not null,
  created_at  timestamptz not null default now()
);

-- One per attempted trade, whether or not a trade results.
create table checklist_entries (
  id                   uuid primary key default gen_random_uuid(),
  session_date         date not null,
  trade_number         integer not null check (trade_number in (1, 2)),
  htf_bias             text not null check (htf_bias in ('bullish', 'bearish', 'neutral')),
  htf_bias_overridden  boolean not null default false,
  amd_phase            text not null check (amd_phase in ('accumulation', 'manipulation', 'distribution', 'unclear')),
  conviction           integer not null check (conviction between 1 and 10),
  entry_confirmation   text not null,
  rule_version         integer not null references rule_versions (version),
  rule_violations      text[] not null default '{}',
  created_at           timestamptz not null default now()
);
create index checklist_entries_session_date_idx on checklist_entries (session_date);

-- Live micro-notes; manual and buddy notes share the table, never merged in stats.
create table notes (
  id                uuid primary key default gen_random_uuid(),
  body              text not null,
  captured_at       timestamptz not null,
  source            text not null check (source in ('desktop', 'phone', 'buddy')),
  matched_trade_id  uuid references trades (id) on delete set null,
  tags              text[] not null default '{}',
  created_at        timestamptz not null default now()
);
create index notes_captured_at_idx on notes (captured_at);

-- Controlled vocabulary, facet-scoped. Seeded below; edited only in Settings.
create table tags (
  id     uuid primary key default gen_random_uuid(),
  facet  text not null check (facet in ('location', 'context', 'trigger', 'management')),
  label  text not null,
  active boolean not null default true,
  unique (facet, label)
);

create table trade_tags (
  trade_id  uuid not null references trades (id) on delete cascade,
  tag_id    uuid not null references tags (id),
  primary key (trade_id, tag_id)
);

-- Screenshots; file lives in Supabase Storage, private bucket 'attachments'.
create table attachments (
  id            uuid primary key default gen_random_uuid(),
  trade_id      uuid not null references trades (id) on delete cascade,
  storage_path  text not null,
  caption       text,
  created_at    timestamptz not null default now()
);

-- Facet taxonomy seed (spec §11).
insert into tags (facet, label) values
  ('location', 'HTF level'),
  ('location', 'range extreme'),
  ('location', 'value/VWAP area'),
  ('location', 'discount/premium leg'),
  ('location', 'no-man''s-land'),
  ('context', 'with HTF bias'),
  ('context', 'counter-trend'),
  ('context', 'no clear bias'),
  ('context', 'open'),
  ('context', 'mid'),
  ('context', 'PM'),
  ('trigger', 'sweep + reversal'),
  ('trigger', 'break + retest'),
  ('trigger', 'momentum continuation'),
  ('trigger', 'exhaustion'),
  ('trigger', 'anticipatory / no trigger'),
  ('management', 'as planned'),
  ('management', 'moved stop'),
  ('management', 'cut early'),
  ('management', 'added'),
  ('management', 'held past target');

-- Rule set v1 (spec §10): max 2 trades/day, conviction floor 8 (stamped, never blocked).
insert into rule_versions (version, rules_json) values
  (1, '{"max_trades_per_day": 2, "conviction_floor": 8}');

-- Single-user app: no client-side table access. Server routes use the service key,
-- which bypasses RLS; enabling RLS with no policies denies anon/authenticated roles.
alter table fills enable row level security;
alter table trades enable row level security;
alter table sessions enable row level security;
alter table rule_versions enable row level security;
alter table checklist_entries enable row level security;
alter table notes enable row level security;
alter table tags enable row level security;
alter table trade_tags enable row level security;
alter table attachments enable row level security;
