-- ─────────────────────────────────────────────
-- PSX PROFIT TRACKER — SUPABASE DATABASE SCHEMA
-- Run this in Supabase SQL Editor
-- ─────────────────────────────────────────────

-- Enable UUID
create extension if not exists "uuid-ossp";

-- ─── STATEMENTS TABLE ───────────────────────
create table public.statements (
  id                  uuid default uuid_generate_v4() primary key,
  user_id             uuid references auth.users(id) on delete cascade,
  statement_id        text not null,
  unique_statement_id text,
  trade_date          date,
  settlement_date     text,
  client_name         text,
  cdc_id              text,
  created_at          timestamptz default now(),
  unique(user_id, unique_statement_id)
);

-- ─── TRADES TABLE ───────────────────────────
create table public.trades (
  id                  uuid default uuid_generate_v4() primary key,
  user_id             uuid references auth.users(id) on delete cascade,
  statement_db_id     uuid references public.statements(id) on delete cascade,
  statement_id        text,
  unique_statement_id text,
  trade_date          date,
  symbol              text not null,
  company_name        text,
  trade_type          text not null check (trade_type in ('BUY', 'SELL')),
  settlement_type     text,
  quantity            integer,
  rate                numeric(12,4),
  commission          numeric(10,4),
  sst                 numeric(10,4),
  cdc                 numeric(10,4),
  cvt_wht             numeric(10,4),
  others              numeric(10,4),
  laga                numeric(10,4),
  secp                numeric(10,4),
  ncs                 numeric(10,4),
  total_charges       numeric(12,4),
  gross_amount        numeric(14,4),
  net_amount          numeric(14,4),
  is_short_sell       boolean default false,
  matched             boolean default false,
  pair_id             uuid,
  net_pl              numeric(14,4),
  gross_pl        numeric(14,4),
  created_at      timestamptz default now()
);

-- ─── AUTO DELETE TRADES OLDER THAN 1 YEAR ───
-- Run this as a scheduled function in Supabase
-- Or set up a cron job via pg_cron
create or replace function delete_old_trades()
returns void as $$
begin
  delete from public.trades
  where trade_date < current_date - interval '1 year';
  
  delete from public.statements
  where trade_date < current_date - interval '1 year';
end;
$$ language plpgsql;

-- ─── ROW LEVEL SECURITY ─────────────────────
-- Users can only see their own data

alter table public.statements enable row level security;
alter table public.trades enable row level security;

-- Statements RLS
create policy "Users see own statements"
  on public.statements for all
  using (auth.uid() = user_id);

-- Trades RLS  
create policy "Users see own trades"
  on public.trades for all
  using (auth.uid() = user_id);

-- ─── INDEXES FOR PERFORMANCE ────────────────
create index idx_trades_user_date on public.trades(user_id, trade_date);
create index idx_trades_symbol on public.trades(user_id, symbol);
create index idx_trades_pair on public.trades(pair_id);
create index idx_trades_matched on public.trades(user_id, matched);
create index idx_statements_user on public.statements(user_id);
