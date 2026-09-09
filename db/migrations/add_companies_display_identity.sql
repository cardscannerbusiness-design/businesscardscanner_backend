-- CMS business Display Name + Display Picture (profile / WhatsApp identity).
ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS display_name VARCHAR(255) NOT NULL DEFAULT '';

ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS display_picture_url TEXT NOT NULL DEFAULT '';
