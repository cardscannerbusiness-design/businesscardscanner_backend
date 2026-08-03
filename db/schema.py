"""Database schema — CREATE TABLE statements for the RBAC auth system."""

from __future__ import annotations

import logging

from db.pool import db_cursor

logger = logging.getLogger(__name__)

# Keep DDL defaults aligned with StorageService seed constants (single source).
from services.storage_service import DEFAULT_PLAN_NAME, DEFAULT_STORAGE_LIMIT_BYTES

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS: list[str] = [
    # ── Extensions (UUID helpers) ──────────────────────────────────────────
    'CREATE EXTENSION IF NOT EXISTS "pgcrypto";',
    # ── Roles ──────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS roles (
        id          SERIAL PRIMARY KEY,
        name        VARCHAR(64)  NOT NULL UNIQUE,
        description VARCHAR(255) NOT NULL DEFAULT '',
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    # ── Permissions ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS permissions (
        id          SERIAL PRIMARY KEY,
        name        VARCHAR(128) NOT NULL UNIQUE,
        description VARCHAR(255) NOT NULL DEFAULT '',
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    # ── Role ↔ Permission mapping ──────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS role_permissions (
        role_id       INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
        PRIMARY KEY (role_id, permission_id)
    );
    """,
    # ── Companies ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS companies (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        company_name  VARCHAR(255) NOT NULL,
        company_code  VARCHAR(64)  NOT NULL UNIQUE,
        admin_id      UUID,               -- set after the admin user is created
        address       VARCHAR(512) NOT NULL DEFAULT '',
        phone         VARCHAR(64)  NOT NULL DEFAULT '',
        email         VARCHAR(255) NOT NULL DEFAULT '',
        website       VARCHAR(512) NOT NULL DEFAULT '',
        status        VARCHAR(32)  NOT NULL DEFAULT 'active',
        created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    # ── Users ──────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS users (
        id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        uuid                   UUID         NOT NULL DEFAULT gen_random_uuid() UNIQUE,
        first_name             VARCHAR(128) NOT NULL DEFAULT '',
        last_name              VARCHAR(128) NOT NULL DEFAULT '',
        email                  VARCHAR(255) NOT NULL UNIQUE,
        username               VARCHAR(128) NOT NULL UNIQUE,
        password_hash          VARCHAR(255) NOT NULL,
        phone                  VARCHAR(64)  NOT NULL DEFAULT '',
        designation            VARCHAR(255) NOT NULL DEFAULT '',
        department             VARCHAR(255) NOT NULL DEFAULT '',
        role_id                INTEGER      NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
        company_id             UUID         REFERENCES companies(id) ON DELETE SET NULL,
        admin_id               UUID,        -- the Admin who created this user (nullable for SuperAdmin)
        profile_image          TEXT,
        is_active              BOOLEAN      NOT NULL DEFAULT TRUE,
        is_verified            BOOLEAN      NOT NULL DEFAULT FALSE,
        failed_login_attempts  INTEGER      NOT NULL DEFAULT 0,
        locked_until           TIMESTAMPTZ,
        last_login             TIMESTAMPTZ,
        last_password_change   TIMESTAMPTZ,
        created_by             UUID,
        updated_by             UUID,
        created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        deleted_at             TIMESTAMPTZ
    );
    """,
    # Profile fields for invited admins/users (idempotent for existing DBs)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS designation VARCHAR(255) NOT NULL DEFAULT '';",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS department VARCHAR(255) NOT NULL DEFAULT '';",
    # Role-based Google Sheets: one workbook per company (Admin); Super Admin sheet on users
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS google_sheet_id VARCHAR(128);",
    # Company storage quota (defaults from StorageService constants)
    f"ALTER TABLE companies ADD COLUMN IF NOT EXISTS plan_name VARCHAR(64) NOT NULL DEFAULT '{DEFAULT_PLAN_NAME}';",
    f"ALTER TABLE companies ADD COLUMN IF NOT EXISTS storage_limit_bytes BIGINT NOT NULL DEFAULT {int(DEFAULT_STORAGE_LIMIT_BYTES)};",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS used_storage_bytes BIGINT NOT NULL DEFAULT 0;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_sheet_id VARCHAR(128);",
    # Admin / Super Admin Google OAuth (create sheets in their own Drive — free)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_refresh_token TEXT;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_connected_email VARCHAR(255);",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_connected_at TIMESTAMPTZ;",
    # ── Offline queue registry ─────────────────────────────────────────────
    # IndexedDB remains the source of truth for synchronization. This table is
    # a best-effort online mirror so SuperAdmin can inspect queues platform-wide.   
    """
    CREATE TABLE IF NOT EXISTS offline_queue_records (
        id                  BIGSERIAL PRIMARY KEY,
        queue_id            VARCHAR(128) NOT NULL,
        created_by_user_id  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        owner_company_id    UUID REFERENCES companies(id) ON DELETE SET NULL,
        status              VARCHAR(32) NOT NULL DEFAULT 'pending',
        retry_count         INTEGER NOT NULL DEFAULT 0,
        contact_data        JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message       TEXT,
        queued_at           TIMESTAMPTZ NOT NULL,
        last_attempt        TIMESTAMPTZ,
        reported_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (queue_id, created_by_user_id)
    );
    """,
    # FK from companies.admin_id → users.id (added after both tables exist)
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE constraint_name = 'companies_admin_id_fkey'
        ) THEN
            ALTER TABLE companies
                ADD CONSTRAINT companies_admin_id_fkey
                FOREIGN KEY (admin_id) REFERENCES users(id) ON DELETE SET NULL;
        END IF;
    END $$;
    """,
    # ── Refresh tokens ─────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS refresh_tokens (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id     UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash  VARCHAR(255) NOT NULL UNIQUE,
        device      VARCHAR(255) NOT NULL DEFAULT '',
        browser     VARCHAR(255) NOT NULL DEFAULT '',
        ip          VARCHAR(45)  NOT NULL DEFAULT '',
        expires_at  TIMESTAMPTZ  NOT NULL,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        revoked_at  TIMESTAMPTZ
    );
    """,
    # ── Sessions ───────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id          UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        refresh_token_id UUID         REFERENCES refresh_tokens(id) ON DELETE SET NULL,
        device           VARCHAR(255) NOT NULL DEFAULT '',
        browser          VARCHAR(255) NOT NULL DEFAULT '',
        ip               VARCHAR(45)  NOT NULL DEFAULT '',
        login_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        last_activity    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        status           VARCHAR(32)  NOT NULL DEFAULT 'active',
        expires_at       TIMESTAMPTZ  NOT NULL
    );
    """,
    # ── Audit logs ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id          BIGSERIAL PRIMARY KEY,
        user_id     UUID,
        action      VARCHAR(128) NOT NULL,
        ip          VARCHAR(45)  NOT NULL DEFAULT '',
        browser     VARCHAR(255) NOT NULL DEFAULT '',
        user_agent  VARCHAR(512) NOT NULL DEFAULT '',
        old_value   JSONB,
        new_value   JSONB,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    # ── Password reset tokens ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id     UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash  VARCHAR(255) NOT NULL,
        otp_code    VARCHAR(16)  NOT NULL,
        expires_at  TIMESTAMPTZ  NOT NULL,
        used_at     TIMESTAMPTZ,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    # ── Email verification tokens ──────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS email_verification_tokens (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id     UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash  VARCHAR(255) NOT NULL,
        new_email   VARCHAR(255) NOT NULL DEFAULT '',
        expires_at  TIMESTAMPTZ  NOT NULL,
        used_at     TIMESTAMPTZ,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    # ── Managed events (Super Admin Event Management) ─────────────────────
    """
    CREATE TABLE IF NOT EXISTS managed_events (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name        VARCHAR(255) NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        location    VARCHAR(512) NOT NULL DEFAULT '',
        start_date  DATE,
        end_date    DATE,
        status      VARCHAR(32)  NOT NULL DEFAULT 'active',
        created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
        updated_by  UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        deleted_at  TIMESTAMPTZ
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_managed_events_status ON managed_events(status) WHERE deleted_at IS NULL;",
    "CREATE INDEX IF NOT EXISTS idx_managed_events_name ON managed_events(name) WHERE deleted_at IS NULL;",
    # ── Indexes ────────────────────────────────────────────────────────────
    "CREATE INDEX IF NOT EXISTS idx_users_email        ON users(email);",
    "CREATE INDEX IF NOT EXISTS idx_users_username     ON users(username);",
    "CREATE INDEX IF NOT EXISTS idx_users_company_id   ON users(company_id);",
    "CREATE INDEX IF NOT EXISTS idx_offline_queue_user ON offline_queue_records(created_by_user_id);",
    "CREATE INDEX IF NOT EXISTS idx_offline_queue_company ON offline_queue_records(owner_company_id);",
    "CREATE INDEX IF NOT EXISTS idx_offline_queue_status ON offline_queue_records(status);",
    "CREATE INDEX IF NOT EXISTS idx_users_role_id      ON users(role_id);",
    "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user       ON sessions(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_user     ON audit_logs(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_action   ON audit_logs(action);",
    # ── Contacts (was previously created by Prisma db:push; now owned here) ─
    """
    CREATE TABLE IF NOT EXISTS contacts (
        id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        "fullName"          TEXT NOT NULL DEFAULT '',
        "firstName"         TEXT NOT NULL DEFAULT '',
        "lastName"          TEXT NOT NULL DEFAULT '',
        designation         TEXT NOT NULL DEFAULT '',
        company             TEXT NOT NULL DEFAULT '',
        phone               TEXT NOT NULL DEFAULT '',
        "secondaryPhone"    TEXT NOT NULL DEFAULT '',
        "countryCode"       TEXT NOT NULL DEFAULT '',
        "countryName"       TEXT NOT NULL DEFAULT '',
        email               TEXT NOT NULL DEFAULT '',
        "secondaryEmail"    TEXT NOT NULL DEFAULT '',
        website             TEXT NOT NULL DEFAULT '',
        "secondaryWebsite"  TEXT NOT NULL DEFAULT '',
        address             TEXT NOT NULL DEFAULT '',
        "secondaryAddress"  TEXT NOT NULL DEFAULT '',
        "socialLinks"       TEXT NOT NULL DEFAULT '',
        "gstNumber"         TEXT NOT NULL DEFAULT '',
        notes               TEXT NOT NULL DEFAULT '',
        "eventName"         TEXT NOT NULL DEFAULT '',
        "eventDay"          TEXT NOT NULL DEFAULT 'Day 1',
        "eventId"           TEXT,
        "cardImageBase64"   TEXT,
        "syncStatus"        TEXT NOT NULL DEFAULT 'synced',
        "createdAt"         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "updatedAt"         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        is_deleted          BOOLEAN NOT NULL DEFAULT FALSE,
        deleted_at          TIMESTAMPTZ,
        created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
        owner_company_id    UUID REFERENCES companies(id) ON DELETE SET NULL,
        created_by_role     VARCHAR(64) NOT NULL DEFAULT '',
        email_delivery_status VARCHAR(32),
        email_delivery_error  TEXT,
        whatsapp_delivery_status VARCHAR(32),
        whatsapp_delivery_error  TEXT
    );
    """,
    # ── Contacts: soft-delete columns (idempotent for older DBs) ───────────
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'contacts'
        ) THEN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'contacts' AND column_name = 'is_deleted'
            ) THEN
                ALTER TABLE contacts ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'contacts' AND column_name = 'deleted_at'
            ) THEN
                ALTER TABLE contacts ADD COLUMN deleted_at TIMESTAMPTZ;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'contacts' AND column_name = 'created_by_user_id'
            ) THEN
                ALTER TABLE contacts ADD COLUMN created_by_user_id UUID;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'contacts' AND column_name = 'eventName'
            ) THEN
                ALTER TABLE contacts ADD COLUMN "eventName" TEXT NOT NULL DEFAULT '';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'contacts' AND column_name = 'eventId'
            ) THEN
                ALTER TABLE contacts ADD COLUMN "eventId" TEXT;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'contacts' AND column_name = 'owner_company_id'
            ) THEN
                ALTER TABLE contacts ADD COLUMN owner_company_id UUID;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'contacts' AND column_name = 'created_by_role'
            ) THEN
                ALTER TABLE contacts ADD COLUMN created_by_role VARCHAR(64) NOT NULL DEFAULT '';
            END IF;
        END IF;
    END $$;
    """,
    # Remap legacy Zoho / local_only values: rows already in PostgreSQL are synced.
    """
    UPDATE contacts
    SET "syncStatus" = 'synced'
    WHERE "syncStatus" IN ('synced_zoho', 'local_only');
    """,
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'contacts'
              AND column_name = 'zohoLeadId'
        ) THEN
            UPDATE contacts
            SET "syncStatus" = 'synced'
            WHERE "zohoLeadId" IS NOT NULL AND "zohoLeadId" <> '';
        END IF;
    END $$;
    """,
    'ALTER TABLE contacts DROP COLUMN IF EXISTS "firebaseId";',
    'ALTER TABLE contacts DROP COLUMN IF EXISTS "zohoLeadId";',
    "ALTER TABLE contacts ALTER COLUMN \"syncStatus\" SET DEFAULT 'synced';",
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS email_delivery_status VARCHAR(32);",
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS email_delivery_error TEXT;",
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS whatsapp_delivery_status VARCHAR(32);",
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS whatsapp_delivery_error TEXT;",
    'ALTER TABLE contacts ADD COLUMN IF NOT EXISTS "cardImageBase64" TEXT;',
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS image_size_bytes BIGINT NOT NULL DEFAULT 0;",
    "CREATE INDEX IF NOT EXISTS idx_contacts_is_deleted ON contacts(is_deleted);",
    "CREATE INDEX IF NOT EXISTS idx_contacts_created_by ON contacts(created_by_user_id);",
    "CREATE INDEX IF NOT EXISTS idx_contacts_owner_company ON contacts(owner_company_id);",
    # Speeds company-scoped active-contact / storage aggregate lookups.
    "CREATE INDEX IF NOT EXISTS idx_contacts_owner_company_active ON contacts(owner_company_id) WHERE (is_deleted = FALSE OR is_deleted IS NULL);",
    "CREATE INDEX IF NOT EXISTS idx_contacts_created_at ON contacts(\"createdAt\");",
    'CREATE INDEX IF NOT EXISTS idx_contacts_event_name ON contacts("eventName");',
    # ── Contacts: country + event day (idempotent) ─────────────────────────
    'ALTER TABLE contacts ADD COLUMN IF NOT EXISTS "countryCode" TEXT NOT NULL DEFAULT \'\';',
    'ALTER TABLE contacts ADD COLUMN IF NOT EXISTS "countryName" TEXT NOT NULL DEFAULT \'\';',
    'ALTER TABLE contacts ADD COLUMN IF NOT EXISTS "eventDay" TEXT NOT NULL DEFAULT \'Day 1\';',
    'CREATE INDEX IF NOT EXISTS idx_contacts_event_day ON contacts("eventDay");',
    # ── Invitations (secure invite-based onboarding) ───────────────────────
    """
    CREATE TABLE IF NOT EXISTS invitations (
        id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email              VARCHAR(255) NOT NULL,
        role               VARCHAR(64)  NOT NULL,
        company_id         UUID         REFERENCES companies(id) ON DELETE SET NULL,
        company_name       VARCHAR(255) NOT NULL DEFAULT '',
        company_code       VARCHAR(64)  NOT NULL DEFAULT '',
        company_address    TEXT         NOT NULL DEFAULT '',
        company_phone      VARCHAR(64)  NOT NULL DEFAULT '',
        company_email      VARCHAR(255) NOT NULL DEFAULT '',
        company_website    VARCHAR(255) NOT NULL DEFAULT '',
        invited_by         UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash         VARCHAR(255) NOT NULL UNIQUE,
        status             VARCHAR(32)  NOT NULL DEFAULT 'pending',
        expires_at         TIMESTAMPTZ  NOT NULL,
        used_at            TIMESTAMPTZ,
        revoked_at         TIMESTAMPTZ,
        created_user_id    UUID         REFERENCES users(id) ON DELETE SET NULL,
        created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_invitations_email ON invitations(email);",
    "CREATE INDEX IF NOT EXISTS idx_invitations_status ON invitations(status);",
    "CREATE INDEX IF NOT EXISTS idx_invitations_invited_by ON invitations(invited_by);",
    "CREATE INDEX IF NOT EXISTS idx_invitations_token_hash ON invitations(token_hash);",
    "ALTER TABLE invitations ADD COLUMN IF NOT EXISTS company_address TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE invitations ADD COLUMN IF NOT EXISTS company_phone VARCHAR(64) NOT NULL DEFAULT '';",
    "ALTER TABLE invitations ADD COLUMN IF NOT EXISTS company_email VARCHAR(255) NOT NULL DEFAULT '';",
    "ALTER TABLE invitations ADD COLUMN IF NOT EXISTS company_website VARCHAR(255) NOT NULL DEFAULT '';",
]


def ensure_schema() -> None:
    """Create all auth-related tables if they do not yet exist."""
    with db_cursor(commit=True) as cur:
        for stmt in SCHEMA_STATEMENTS:
            cur.execute(stmt)
    logger.info("Database schema ensured (auth tables created if missing).")
