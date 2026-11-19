# Resend setup (manual steps - nothing here is done by the code)

Flintel sends sign-up codes through the **Resend API** and receives delivery events through a **signed webhook**.
These steps are done by a person in the Resend dashboard, at your DNS provider and in your host's environment. Dashboard wording may differ slightly from
what is written here; the Resend documentation is authoritative.

## 1. Environment variables (set in the host's environment, never in git)

| Variable | Value |
|---|---|
| `RESEND_API_KEY` | An API key from Resend. Prefer a key with **sending access only**, restricted to your sending domain. |
| `EMAIL_FROM` | `Flintel <no-reply@mail.your-domain.example>` - an address on the domain you verify in step 2. |
| `RESEND_WEBHOOK_SECRET` | The webhook **signing secret** (starts with `whsec_`) from step 4. |

Remove the old `SMTP_*` and `EMAIL_BACKEND=smtp` settings: SMTP is no longer used and `EMAIL_BACKEND=smtp` is refused with a clear log message.
Missing settings never crash the app: sign-up answers "temporarily unavailable" (no `RESEND_API_KEY`/`EMAIL_FROM`) and the webhook answers 503 (no
`RESEND_WEBHOOK_SECRET`). The log names the missing variable, never a value.

## 2. Verify the sending domain (DNS - at your DNS provider)

1. Resend dashboard -> Domains -> add your sending domain (a subdomain such as `mail.your-domain.example` keeps your main domain's reputation separate).
2. Add the DNS records Resend shows (SPF and DKIM) exactly as given, then wait until Resend shows the domain as **verified**.
3. Add a DMARC record for the domain if you have none, for example `_dmarc.your-domain.example  TXT  "v=DMARC1; p=none; rua=mailto:you@your-domain.example"`.
   Start with `p=none`, read the reports, and tighten the policy later. Gmail expects SPF and DKIM to pass and aligned with the From domain.
4. `EMAIL_FROM` must use this verified domain. Sending from an unverified domain is rejected by Resend (the app reports "unavailable" and logs `kind=invalid_sender`).

## 3. API key

Resend dashboard -> API Keys -> create a key (sending access, restricted to the domain). Put it in `RESEND_API_KEY`. Rotate it if it was ever pasted into chat, a ticket or a commit.

## 4. Webhook

1. Resend dashboard -> Webhooks -> add endpoint: `https://<your-production-host>/webhooks/resend` (HTTPS, publicly reachable; the host must be in `TRUSTED_HOSTS` if you set it).
2. Subscribe to: `email.sent`, `email.delivered`, `email.delivery_delayed`, `email.bounced`, `email.complained`, `email.failed`, `email.suppressed`.
   Other events are acknowledged and ignored.
3. Copy the endpoint's **signing secret** into `RESEND_WEBHOOK_SECRET` and restart the app.
4. The endpoint needs no login or session. Its only credential is the signature over the raw request body; a request with a missing, wrong or stale (over five minutes old) signature is refused with 401.

## 5. After deploying (checklist)

- Request a sign-up code for an address you own. Check the log for `Email accepted by Resend API (not proof of delivery) | email_id=...`.
- In Resend -> Emails, find that email id; in Webhooks -> your endpoint -> recent deliveries, confirm the events were answered with 200.
- A new document appears in the `flintel_email_deliveries` collection for each accepted email, with its state (`accepted` -> `sent` -> `delivered`, or `bounced` / `failed` / `suppressed` / `complained`). Records are purged after 30 days.

## 6. What the states mean (and do not mean)

- `accepted`: Resend's API accepted the request. Not delivery.
- `delivered`: the **receiving mail server** accepted the message. It does **not** mean the message is in the inbox rather than spam, and no provider can report inbox placement.
- `bounced`, `failed`, `suppressed`, `complained`: problems. Flintel only records and logs them. It does **not** resend, retry, or change Resend's suppression list.
  If a recipient is suppressed, review it in Resend -> Suppressions and remove it there yourself only once the cause is understood.

## 7. Privacy note

`/signup/code` answers identically whether or not the address already has an account. An address that already has one receives an "you already have a
Flintel account" email instead of a code, within the same cooldown and hourly limits as a code email.
