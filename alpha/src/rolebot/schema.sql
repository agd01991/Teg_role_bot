CREATE SCHEMA IF NOT EXISTS rolebot;
SET search_path TO rolebot, pg_catalog;
CREATE TABLE IF NOT EXISTS meta (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version integer NOT NULL DEFAULT 1 CHECK (version = 1),
    bot_id bigint,
    ledger_id text,
    ledger_seq bigint NOT NULL DEFAULT 0,
    ledger_hash text NOT NULL DEFAULT ''
);
INSERT INTO meta(singleton) VALUES(true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS chats (id bigint PRIMARY KEY CHECK(id < 0), enabled boolean NOT NULL);
CREATE TABLE IF NOT EXISTS people (
    id bigint PRIMARY KEY CHECK(id > 0),
    blocked boolean NOT NULL DEFAULT false,
    ever_deleted boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS members (
    chat bigint NOT NULL REFERENCES chats(id),
    person bigint NOT NULL REFERENCES people(id),
    name text NOT NULL DEFAULT '' CHECK(length(name) <= 48),
    active boolean NOT NULL DEFAULT false,
    opt_out boolean NOT NULL DEFAULT false,
    admin_revoked boolean NOT NULL DEFAULT false,
    revision bigint NOT NULL DEFAULT 0,
    last_event bigint NOT NULL DEFAULT -1,
    PRIMARY KEY(chat, person)
);
CREATE TABLE IF NOT EXISTS roles (
    id bigserial PRIMARY KEY,
    chat bigint NOT NULL REFERENCES chats(id),
    code text NOT NULL CHECK(length(code) BETWEEN 2 AND 32),
    title text NOT NULL CHECK(length(title) BETWEEN 1 AND 64),
    version integer NOT NULL DEFAULT 1,
    UNIQUE(chat, code), UNIQUE(chat, id)
);
CREATE TABLE IF NOT EXISTS assignments (
    chat bigint NOT NULL, role bigint NOT NULL, person bigint NOT NULL,
    PRIMARY KEY(chat, role, person),
    FOREIGN KEY(chat, role) REFERENCES roles(chat, id) ON DELETE CASCADE,
    FOREIGN KEY(chat, person) REFERENCES members(chat, person) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS pending (
    digest text PRIMARY KEY,
    chat bigint NOT NULL REFERENCES chats(id),
    author bigint NOT NULL REFERENCES people(id),
    code text NOT NULL, title text NOT NULL,
    expires timestamptz NOT NULL DEFAULT now() + interval '2 minutes',
    UNIQUE(chat, author)
);
CREATE TABLE IF NOT EXISTS inbox (
    update_id bigint PRIMARY KEY, chat bigint, message_id bigint, created timestamptz NOT NULL DEFAULT now(), UNIQUE(chat,message_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id bigserial PRIMARY KEY,
    chat bigint NOT NULL REFERENCES chats(id),
    author bigint NOT NULL REFERENCES people(id),
    author_revision bigint NOT NULL,
    role bigint NOT NULL,
    role_version integer NOT NULL,
    update_id bigint NOT NULL UNIQUE,
    message_id bigint NOT NULL,
    thread_id bigint,
    created timestamptz NOT NULL DEFAULT now(),
    not_before timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','done','cancelled','failed','uncertain')),
    FOREIGN KEY(chat, role) REFERENCES roles(chat, id),
    UNIQUE(chat,message_id)
);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(not_before, id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS jobs_recent_chat ON jobs(chat, created);
CREATE INDEX IF NOT EXISTS jobs_recent_author ON jobs(chat, author, created);
CREATE INDEX IF NOT EXISTS jobs_recent_role ON jobs(chat, role, created);
CREATE TABLE IF NOT EXISTS parts (
    job bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    number integer NOT NULL,
    recipients bigint[] NOT NULL CHECK(cardinality(recipients) <= 5),
    epochs jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sending','sent','skipped','failed','uncertain')),
    attempts integer NOT NULL DEFAULT 0,
    telegram_id bigint,
    PRIMARY KEY(job, number)
);
CREATE TABLE IF NOT EXISTS audit (
    id bigserial PRIMARY KEY,
    chat bigint NOT NULL REFERENCES chats(id),
    actor bigint,
    action text NOT NULL,
    created timestamptz NOT NULL DEFAULT now()
);
