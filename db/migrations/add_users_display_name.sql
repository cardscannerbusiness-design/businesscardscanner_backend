-- Per-user Display Name (independent of companies.display_name / email_display_name).
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS display_name VARCHAR(255) NOT NULL DEFAULT '';
