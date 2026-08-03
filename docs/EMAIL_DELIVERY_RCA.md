# Email delivery — root cause & operations notes

## Symptom

Gmail reports:

> Delivery Incomplete — The recipient server did not accept our requests to connect.
> [ulavi.com &lt;IP&gt;: timed out]

## Root cause (confirmed)

**`ulavi.com` has no MX DNS records.**

Evidence (2026-08-03):

| Check | Result |
|-------|--------|
| `Resolve-DnsName ulavi.com -Type MX` | No MX answers (SOA only) |
| A record `ulavi.com` | `208.98.35.186` |
| TCP 25 / 465 / 587 / 993 to that IP | **All timeout** |

When a domain has no MX, mail servers fall back to the A record and try port 25.
That host does not accept SMTP → sender (Gmail or any MTA) times out and sends
“Delivery Incomplete”.

This is **not** caused by NameCardScan application code failing to submit mail.
Outbound SMTP to Gmail (`smtp.gmail.com:587`) and Amazon SES
(`email-smtp.us-east-1.amazonaws.com:587`) are reachable from the app host.

### Contrast: domains that *can* receive mail

- `namecardscan.com` — MX → GoDaddy (`smtp.secureserver.net`)
- `ulacab.com` — MX → Google + Zoho
- `ulavitech.com` — MX → Google
- `gmail.com` / Outlook / Yahoo — normal MX

## App-side fix applied

Local/backend config uses Amazon SES:

```
SMTP_HOST=email-smtp.us-east-1.amazonaws.com
SMTP_USER=AKIA…          # IAM SMTP username (NOT an email)
SMTP_FROM=…@ulacab.com   # verified SES identity (must be From)
```

Previously, thank-you mail used `SMTP_USER` as the From header. For SES that is an
IAM access key, which is invalid as a From address and breaks delivery.

**Fix:** `smtp_sender_email()` now uses `SMTP_FROM` / `BUSINESS_EMAIL` when the
transport is SES (or when `SMTP_USER` is not an email). Auth emails use the same rule.

Also added a pre-send MX check: if the recipient domain has **zero** MX records,
the API fails fast with a clear error instead of accepting mail that will bounce later.

## Required infrastructure fixes (outside the app)

### 1. Receiving `@ulavi.com` mail

In the DNS panel for `ulavi.com` (currently `ns1.site4now.net` / SmarterASP):

1. Add **MX** records for your real mail provider (Google Workspace, Microsoft 365,
   Zoho, cPanel, etc.).
2. Open inbound SMTP on the mail host (port 25 from the internet, or use the
   provider’s MX hosts).
3. Publish SPF / DKIM / DMARC for that provider.

Until MX exists and port 25 answers, **no** sender (Gmail, SES, Outlook) can
deliver to `*@ulavi.com`.

### 2. Sending via Amazon SES as `@ulacab.com`

In SES (us-east-1):

1. Verify identity `SMTP_FROM` (and domain `ulacab.com` if possible).
2. Move out of sandbox (or verify each recipient) for production volume.
3. Update SPF for `ulacab.com` to include SES, e.g.:

   `v=spf1 include:amazonses.com include:_spf.google.com include:zoho.com ~all`

   (Consolidate duplicate SPF TXT records — multiple `v=spf1` strings are invalid.)

4. Ensure production EC2 `.env` matches:

   - `SMTP_HOST=email-smtp.us-east-1.amazonaws.com`
   - `SMTP_PORT=587`
   - `SMTP_USER` / `SMTP_PASSWORD` = SES SMTP credentials
   - `SMTP_FROM` = verified address (never the AKIA key)
   - `BUSINESS_EMAIL` = reply-to / public contact

### 3. If production still uses Gmail SMTP

Gmail can accept the message and then fail later when delivering to `ulavi.com`.
That produces exactly the Gmail “Delivery Incomplete” notification. Prefer SES
with a verified domain for production thank-you mail.

## Verification checklist

1. Restart backend after deploying the From/MX fixes.
2. Send a test thank-you to a **Gmail** address → should arrive.
3. Send to **Outlook / Yahoo** → should arrive (watch spam).
4. Send to `*@ulavi.com` → should **fail fast** with MX error until DNS is fixed.
5. After adding MX for `ulavi.com`, re-test and confirm no Gmail timeout bounce.
