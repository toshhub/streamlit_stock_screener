-- Personal cloud data for the stock screener.
-- Run this once in Supabase Dashboard > SQL Editor.
--
-- Google authentication is handled by Streamlit OIDC. The verified Google
-- `sub` claim is stored as user_id. Only the Streamlit server uses the
-- service-role key; browser/anon access to these tables is intentionally denied.

create table if not exists public.user_filter_sets (
    user_id text not null,
    name text not null check (char_length(name) between 1 and 120),
    filter_data jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (user_id, name)
);

create table if not exists public.user_settings (
    user_id text primary key,
    settings jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);

create table if not exists public.user_alerts (
    user_id text not null,
    id text not null,
    market text not null check (market in ('INDIA', 'US')),
    symbol text not null,
    target_price numeric not null check (target_price > 0),
    direction text not null check (direction in ('above', 'below')),
    status text not null default 'Active' check (status in ('Active', 'Triggered')),
    reference_price numeric not null,
    created_at text not null,
    created_candle_date text not null default '',
    last_checked_date text not null default '',
    triggered_at text not null default '',
    triggered_candle_date text not null default '',
    triggered_price numeric,
    primary key (user_id, id)
);

create index if not exists user_alerts_active_symbol_market_idx
    on public.user_alerts (status, symbol, market);

alter table public.user_filter_sets enable row level security;
alter table public.user_settings enable row level security;
alter table public.user_alerts enable row level security;

-- There are deliberately no anon/authenticated policies. Supabase's service
-- role bypasses RLS from the trusted Streamlit server, and every application
-- query additionally filters on the verified Google user_id.
revoke all on public.user_filter_sets from anon, authenticated;
revoke all on public.user_settings from anon, authenticated;
revoke all on public.user_alerts from anon, authenticated;
