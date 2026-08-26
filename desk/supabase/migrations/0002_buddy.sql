-- Trading Desk — buddy tables (spec §6)

create extension if not exists vector;

create table chat_sessions (
  id           uuid primary key default gen_random_uuid(),
  session_date date not null,
  summary      text,
  updated_at   timestamptz not null default now()
);
create index chat_sessions_date_idx on chat_sessions (session_date);

create table chat_messages (
  id               uuid primary key default gen_random_uuid(),
  chat_session_id  uuid not null references chat_sessions (id) on delete cascade,
  role             text not null check (role in ('user', 'assistant')),
  content          text not null,
  tool_calls_json  jsonb,
  ts               timestamptz not null default now()
);
create index chat_messages_session_idx on chat_messages (chat_session_id, ts);

-- Durable facts. Agent may propose; only Jay's /confirm sets active = true.
create table facts (
  id          uuid primary key default gen_random_uuid(),
  text        text not null,
  source      text not null check (source in ('user', 'agent_proposed')),
  active      boolean not null default false,
  created_at  timestamptz not null default now()
);

-- Behavioral patterns the agent noticed; injected only when relevant.
create table observations (
  id         uuid primary key default gen_random_uuid(),
  text       text not null,
  trade_ids  uuid[] not null default '{}',
  embedding  vector(1536),
  ts         timestamptz not null default now()
);

create table opinions (
  id                  uuid primary key default gen_random_uuid(),
  ts                  timestamptz not null default now(),
  price               numeric,
  question            text not null,
  type                text not null check (type in ('level', 'day', 'manage')),
  verdict             text not null,
  confidence          numeric check (confidence between 0 and 1),
  factors_json        jsonb not null,      -- each factor with validated/discretionary tag
  tool_snapshot_json  jsonb not null,
  trade_id            uuid references trades (id) on delete set null,
  graded_at           timestamptz,
  outcome             text,
  score               numeric
);
create index opinions_ts_idx on opinions (ts);
create index opinions_ungraded_idx on opinions (ts) where graded_at is null;

create table watches (
  id          uuid primary key default gen_random_uuid(),
  price       numeric not null,
  created_at  timestamptz not null default now(),
  active      boolean not null default true
);

create table pings (
  id       uuid primary key default gen_random_uuid(),
  ts       timestamptz not null default now(),
  trigger  text not null,
  body     text not null,
  read     boolean not null default false
);
create index pings_ts_idx on pings (ts);

alter table chat_sessions enable row level security;
alter table chat_messages enable row level security;
alter table facts enable row level security;
alter table observations enable row level security;
alter table opinions enable row level security;
alter table watches enable row level security;
alter table pings enable row level security;
