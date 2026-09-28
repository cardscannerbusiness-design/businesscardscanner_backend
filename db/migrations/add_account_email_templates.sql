CREATE TABLE IF NOT EXISTS email_templates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    admin_user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    template_name   VARCHAR(160) NOT NULL,
    template_key    VARCHAR(64) NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    components      JSONB NOT NULL DEFAULT '[]'::jsonb,
    event_id        UUID REFERENCES managed_events(id) ON DELETE SET NULL,
    token_map       JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          VARCHAR(16) NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'inactive')),
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMPTZ
);

ALTER TABLE email_templates ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE email_templates ADD COLUMN IF NOT EXISTS components JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE email_templates ADD COLUMN IF NOT EXISTS event_id UUID REFERENCES managed_events(id) ON DELETE SET NULL;
ALTER TABLE email_templates DROP CONSTRAINT IF EXISTS email_templates_account_id_template_key_key;

CREATE INDEX IF NOT EXISTS idx_email_templates_account
    ON email_templates(account_id);
CREATE INDEX IF NOT EXISTS idx_email_templates_admin
    ON email_templates(admin_user_id);
CREATE INDEX IF NOT EXISTS idx_email_templates_active_key
    ON email_templates(account_id, template_key)
    WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_email_templates_account_event_unique
    ON email_templates(account_id, event_id)
    WHERE event_id IS NOT NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_email_templates_account_default_key_unique
    ON email_templates(account_id, template_key)
    WHERE event_id IS NULL AND deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS account_media (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    admin_user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_name       VARCHAR(255) NOT NULL,
    original_name   VARCHAR(255) NOT NULL DEFAULT '',
    media_kind      VARCHAR(32) NOT NULL
                        CHECK (media_kind IN ('image', 'video', 'document')),
    mime_type       VARCHAR(128) NOT NULL,
    storage_key     TEXT NOT NULL,
    public_url      TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL DEFAULT 0,
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMPTZ,
    UNIQUE (account_id, storage_key)
);
CREATE INDEX IF NOT EXISTS idx_account_media_account ON account_media(account_id);
CREATE INDEX IF NOT EXISTS idx_account_media_admin ON account_media(admin_user_id);

INSERT INTO email_templates (
    account_id, admin_user_id, template_name, template_key,
    subject, body, token_map, status, created_by, updated_by
)
SELECT
    u.company_id,
    s.admin_user_id,
    'Business Card Follow-up',
    'CARD_FOLLOW_UP',
    COALESCE(s.templates->>'email_subject', ''),
    COALESCE(s.templates->>'email_body', ''),
    CASE
        WHEN jsonb_typeof(s.templates->'token_map') = 'object'
            THEN s.templates->'token_map'
        ELSE '{}'::jsonb
    END,
    'active',
    s.updated_by,
    s.updated_by
FROM admin_env_settings s
JOIN users u ON u.id = s.admin_user_id
WHERE u.company_id IS NOT NULL
  AND (
      COALESCE(s.templates->>'email_subject', '') <> ''
      OR COALESCE(s.templates->>'email_body', '') <> ''
  )
  AND NOT EXISTS (
      SELECT 1
      FROM email_templates existing
      WHERE existing.account_id = u.company_id
        AND existing.template_key = 'CARD_FOLLOW_UP'
        AND existing.event_id IS NULL
        AND existing.deleted_at IS NULL
  );
