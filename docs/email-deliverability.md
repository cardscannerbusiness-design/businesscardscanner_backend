# Email deliverability guide

## Current transport

NameCardScan sends mail over **authenticated SMTP** (`services/email_service.py` + `auth/email_service.py`).

There is **no built-in Brevo API or Amazon SES API client**. You can still use those providers by pointing SMTP at their relays:

| Provider | Typical SMTP host | Notes |
|----------|-------------------|--------|
| Gmail / Google Workspace | `smtp.gmail.com:587` | Fine for low volume; cold outreach often lands in Junk |
| Amazon SES | `email-smtp.<region>.amazonaws.com:587` | Preferred for production transactional mail |
| Brevo | `smtp-relay.brevo.com:587` | Use verified domain + SMTP key |

## Application behavior (unchanged UX)

Outbound messages include:

- **From / Reply-To alignment** — From uses the authenticated SMTP mailbox when its domain differs from `BUSINESS_EMAIL` / `SMTP_FROM`; Reply-To stays on the business address
- Optional **MAIL FROM** via `EMAIL_RETURN_PATH` or `SMTP_MAIL_FROM`
- **Message-ID**, **Date**, **MIME-Version**, multipart plain + HTML

Subject lines, email body content, templates, auth flows, and send workflow are unchanged.

### Standalone DNS diagnostic

```bash
python -m services.email_deliverability
```

Checks SPF / DKIM / DMARC and From↔SMTP alignment. This is a CLI utility only — not an API route or admin Settings page.

## DNS checklist (required for inbox placement)

Replace `example.com` with your sending domain (the domain of the authenticated From address).

### SPF

```
example.com.  TXT  "v=spf1 include:amazonses.com ~all"
```

or for Brevo / Google, use the `include:` value from the provider console.

### DKIM

- **SES:** enable Easy DKIM and publish the CNAMEs SES shows.
- **Brevo / Workspace:** publish the selector TXT under `selector._domainkey.example.com`.
- Optional app hint: set `DKIM_SELECTOR=yourselector` so the health check looks up the right name.

### DMARC

```
_dmarc.example.com.  TXT  "v=DMARC1; p=none; rua=mailto:dmarc@example.com; fo=1"
```

Raise `p` to `quarantine` then `reject` after alignment looks clean.

### Custom MAIL FROM (SES)

1. Create a subdomain such as `mail.example.com` in SES.
2. Publish the MX / SPF records SES requires.
3. Set `EMAIL_RETURN_PATH=bounce@mail.example.com` (or your provider’s envelope address).

## Environment variables

| Variable | Purpose |
|----------|---------|
| `SMTP_HOST` / `SMTP_PORT` | Relay host |
| `SMTP_USER` / `SMTP_PASSWORD` | Authenticated SMTP (or `GMAIL_*`) |
| `BUSINESS_EMAIL` | Reply-To / preferred From identity |
| `BUSINESS_COMPANY_NAME` | Display name in From |
| `SMTP_FROM` | Preferred From when domain matches SMTP auth |
| `EMAIL_RETURN_PATH` / `SMTP_MAIL_FROM` | Envelope MAIL FROM |
| `DKIM_SELECTOR` | Extra DKIM selector for health checks |
| `EMAIL_TEST_RECIPIENT` | Redirect all thank-you sends while testing |

## End-to-end inbox tests

Use three fresh (or low-reputation) mailboxes and send the thank-you via **Settings → Email follow-ups on** + scan/save, or `POST /integrations/email/test`.

| Provider | What to check | How |
|----------|---------------|-----|
| **Gmail** | Auth results + spam | Open message → Show original → look for `spf=pass`, `dkim=pass`, `dmarc=pass` |
| **Outlook** | Inbox vs Junk | Message details / headers; Authentication-Results |
| **Yahoo** | Inbox vs Spam | Full header → Authentication-Results |

Record for each send:

1. Authentication results (SPF / DKIM / DMARC)
2. Folder (Inbox / Promotions / Junk)
3. Approximate spam score if available (Gmail does not expose a numeric score; use mail-tester.com for a proxy score)
4. Whether Message-ID / Date / multipart bodies are present

### Suggested mail-tester pass

1. Generate an address at [mail-tester.com](https://www.mail-tester.com/).
2. `POST /integrations/email/test` with that address.
3. Target **score ≥ 8/10** before production cold outreach.

## Remaining recommendations

1. **Stop using consumer Gmail SMTP for production outreach.** Move to SES or Brevo with a verified corporate domain.
2. Warm the domain gradually (low daily volume, consistent From identity).
3. Keep PDF / video links; avoid adding more tracking pixels.
4. First-time recipients will still filter aggressively until domain reputation builds — expect some Junk until SPF/DKIM/DMARC all pass and volume is steady.
