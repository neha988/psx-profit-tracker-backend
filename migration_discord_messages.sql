-- Migration: Add Discord scheduled messages table
-- Run this in Supabase SQL Editor

-- ─── DISCORD MESSAGES TABLE ──────────────────
create table public.discord_messages (
  id                  uuid default uuid_generate_v4() primary key,
  admin_user_id       uuid references auth.users(id) on delete cascade,
  channel_id          text not null check (channel_id in ('1498347963532054768', '1498348133514612746')),
  channel_name        text not null,
  message_format      text not null check (message_format in ('embed', 'plain_text')),
  
  -- Trade details
  trade_date          date not null,
  trade_time          time not null,
  symbol              text not null,
  buy_price           numeric(12,4) not null,
  sell_price          numeric(12,4) not null,
  stop_loss           numeric(12,4) not null,
  difference          numeric(12,4),
  result              text,
  
  -- Scheduling
  scheduled_at        timestamptz not null,
  sent_at             timestamptz,
  status              text default 'scheduled' check (status in ('scheduled', 'sent', 'failed')),
  error_message       text,
  
  created_at          timestamptz default now()
);

-- ─── INDEXES ─────────────────────────────────
create index idx_discord_messages_scheduled on public.discord_messages(status) where status = 'scheduled';
create index idx_discord_messages_admin on public.discord_messages(admin_user_id);
create index idx_discord_messages_scheduled_at on public.discord_messages(scheduled_at);
