CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    telegram_user_id BIGINT PRIMARY KEY,
    full_name TEXT NOT NULL,
    username TEXT,
    profile_link TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'inactive')),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    name_updated_at TIMESTAMPTZ,
    name_updated_by BIGINT,
    CONSTRAINT users_full_name_not_blank CHECK (length(btrim(full_name)) >= 2)
);

CREATE TABLE IF NOT EXISTS admin_group_config (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    chat_id BIGINT NOT NULL,
    thread_id BIGINT,
    configured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    configured_by BIGINT
);

CREATE TABLE IF NOT EXISTS cashier_config (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    target_type TEXT NOT NULL CHECK (target_type IN ('user', 'group')),
    target_chat_id BIGINT NOT NULL,
    label TEXT NOT NULL DEFAULT 'الصندوق',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    configured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    configured_by BIGINT
);

CREATE TABLE IF NOT EXISTS expense_requests (
    id BIGSERIAL PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    user_id BIGINT NOT NULL REFERENCES users(telegram_user_id),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft', 'submitted', 'changes_requested', 'resubmitted',
        'approved_by_admin', 'cashier_review', 'paid_confirmed',
        'payment_rejected', 'partially_paid', 'cancelled_by_admin',
        'cancelled_by_user', 'expired', 'reopened'
    )),
    version_no INTEGER NOT NULL DEFAULT 1 CHECK (version_no > 0),
    mosque_name TEXT NOT NULL,
    wilaya TEXT NOT NULL,
    mission_start_date DATE,
    mission_end_date DATE,
    duration_days INTEGER CHECK (duration_days IS NULL OR duration_days > 0),
    duration_text TEXT NOT NULL,
    amount_requested NUMERIC(14,2) NOT NULL CHECK (amount_requested > 0),
    currency TEXT NOT NULL DEFAULT 'DZD',
    additional_details TEXT NOT NULL DEFAULT '',
    admin_chat_id BIGINT,
    admin_thread_id BIGINT,
    admin_message_id BIGINT,
    approved_by BIGINT,
    approved_at TIMESTAMPTZ,
    approved_version_no INTEGER,
    cashier_chat_id BIGINT,
    paid_amount NUMERIC(14,2) CHECK (paid_amount IS NULL OR paid_amount >= 0),
    payment_method TEXT,
    payment_note TEXT,
    cashier_user_id BIGINT,
    paid_at TIMESTAMPTZ,
    cancel_reason TEXT,
    cancelled_by BIGINT,
    cancelled_at TIMESTAMPTZ,
    rejection_reason TEXT,
    rejected_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (mission_end_date IS NULL OR mission_start_date IS NULL OR mission_end_date >= mission_start_date)
);

CREATE TABLE IF NOT EXISTS request_versions (
    id BIGSERIAL PRIMARY KEY,
    request_id BIGINT NOT NULL REFERENCES expense_requests(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    snapshot JSONB NOT NULL,
    created_by BIGINT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('user', 'admin')),
    change_reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (request_id, version_no)
);

CREATE TABLE IF NOT EXISTS expense_items (
    id BIGSERIAL PRIMARY KEY,
    request_id BIGINT NOT NULL REFERENCES expense_requests(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    description TEXT NOT NULL,
    amount NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL DEFAULT 'DZD',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS attachments (
    id BIGSERIAL PRIMARY KEY,
    request_id BIGINT NOT NULL REFERENCES expense_requests(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    telegram_file_id TEXT NOT NULL,
    telegram_file_unique_id TEXT,
    media_type TEXT NOT NULL,
    original_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS workflow_events (
    id BIGSERIAL PRIMARY KEY,
    request_id BIGINT REFERENCES expense_requests(id) ON DELETE CASCADE,
    target_user_id BIGINT REFERENCES users(telegram_user_id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor_id BIGINT,
    actor_name TEXT,
    version_no INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS delivery_messages (
    id BIGSERIAL PRIMARY KEY,
    request_id BIGINT REFERENCES expense_requests(id) ON DELETE SET NULL,
    recipient_user_id BIGINT,
    recipient_chat_id BIGINT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('private', 'group')),
    message_kind TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_expense_requests_user ON expense_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_expense_requests_status ON expense_requests(status);
CREATE INDEX IF NOT EXISTS idx_expense_requests_created_at ON expense_requests(created_at);
ALTER TABLE workflow_events ADD COLUMN IF NOT EXISTS target_user_id BIGINT REFERENCES users(telegram_user_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_events_request ON workflow_events(request_id, created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_events_user ON workflow_events(target_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_delivery_messages_pending ON delivery_messages(status, created_at);
CREATE INDEX IF NOT EXISTS idx_attachments_request ON attachments(request_id, version_no);

INSERT INTO app_settings (key, value) VALUES
    ('default_currency', 'DZD'),
    ('allow_partial_payment', 'false'),
    ('require_attachment', 'false'),
    ('stale_hours', '72')
ON CONFLICT (key) DO NOTHING;
