CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Категории. tier определяет роль в скрипте продаж:
--   1 = продавать в первую очередь (сеты, пицца, мангал, горячее)
--   2 = допродавать (роллы, салаты, десерты)
--   3 = обязательные добавки (напитки, соусы, гарниры)
CREATE TABLE IF NOT EXISTS categories (
    key         text PRIMARY KEY,
    name        text NOT NULL,
    tier        int  NOT NULL DEFAULT 2 CHECK (tier BETWEEN 1 AND 3),
    addon_kind  text,          -- garnish | sauce | drink | NULL
    sort_order  int  NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS menu_items (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    category_key text NOT NULL REFERENCES categories(key) ON DELETE RESTRICT,
    name         text NOT NULL,
    description  text NOT NULL DEFAULT '',
    price_cents  bigint NOT NULL DEFAULT 0 CHECK (price_cents >= 0),
    is_available boolean NOT NULL DEFAULT true,
    is_hit       boolean NOT NULL DEFAULT false,   -- "сейчас хорошо берут"
    aliases      jsonb NOT NULL DEFAULT '[]'::jsonb,
    sizes        jsonb NOT NULL DEFAULT '[]'::jsonb,
    modifiers    jsonb NOT NULL DEFAULT '[]'::jsonb,
    external_id  text,                              -- id в iiko
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_items_category ON menu_items(category_key);
CREATE INDEX IF NOT EXISTS idx_items_available ON menu_items(is_available);
-- поиск по названию и алиасам
CREATE INDEX IF NOT EXISTS idx_items_name_trgm ON menu_items USING gin (lower(name) gin_trgm_ops);

CREATE TABLE IF NOT EXISTS calls (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel      text NOT NULL DEFAULT 'phone' CHECK (channel IN ('phone','whatsapp')),
    caller       text NOT NULL DEFAULT '',
    room         text NOT NULL DEFAULT '',
    status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','failed','abandoned')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    ended_at     timestamptz,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE TABLE IF NOT EXISTS transcripts (
    id        bigserial PRIMARY KEY,
    call_id   uuid NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    speaker   text NOT NULL,
    text      text NOT NULL,
    at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_transcripts_call ON transcripts(call_id, at);

CREATE TABLE IF NOT EXISTS orders (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    number         bigserial UNIQUE,
    call_id        uuid REFERENCES calls(id) ON DELETE SET NULL,
    channel        text NOT NULL DEFAULT 'phone',
    customer_name  text NOT NULL DEFAULT '',
    phone          text NOT NULL DEFAULT '',
    address        text NOT NULL DEFAULT '',
    persons        int,
    cutlery        boolean,
    payment        text NOT NULL DEFAULT 'cash' CHECK (payment IN ('cash','transfer','card')),
    change_from    bigint,
    comment        text NOT NULL DEFAULT '',
    total_cents    bigint NOT NULL DEFAULT 0,
    status         text NOT NULL DEFAULT 'confirmed'
                   CHECK (status IN ('confirmed','sent_to_kitchen','delivering','completed','cancelled')),
    receipt        jsonb NOT NULL,
    iiko_order_id  text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- Настройки заведения. Одна строка, id=1.
CREATE TABLE IF NOT EXISTS settings (
    id       int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    data     jsonb NOT NULL DEFAULT '{}'::jsonb
);
INSERT INTO settings (id, data) VALUES (1, '{}'::jsonb) ON CONFLICT DO NOTHING;
