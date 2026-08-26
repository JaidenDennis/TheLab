-- Econ calendar backing table for the buddy's calendar tool and the watcher's
-- event-warning trigger. Populated manually (Settings) — no scraping in v1.

create table calendar_events (
  id             uuid primary key default gen_random_uuid(),
  event_date     date not null,
  event_time_et  time not null,
  name           text not null,
  impact         text not null check (impact in ('low', 'medium', 'high')),
  created_at     timestamptz not null default now()
);
create index calendar_events_date_idx on calendar_events (event_date);

alter table calendar_events enable row level security;
