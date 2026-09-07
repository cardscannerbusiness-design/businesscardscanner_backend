-- CMS "Email Display Name" — From header display name only, keyed by company_id.
ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS email_display_name VARCHAR(255) NOT NULL DEFAULT '';
