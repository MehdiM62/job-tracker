# Job Application Tracker

A Streamlit web app that extracts job details from any URL using AI and saves them to a Google Sheet.

**Features**
- Paste any job URL → AI (`openai/gpt-oss-120b` via OpenRouter — with an automatic direct-Groq fallback) extracts company, role, city, skills, contact, and more
- Falls back to manual paste for sites that block scrapers (LinkedIn, etc.)
- Optional job-fit matching against your career profile (Match Level + Missing Skills)
- Source dropdown auto-detected from the URL, with an "Other" option for custom sources
- CV Language toggle (EN / DE)
- Date Applied auto-set to current CET time, editable if you're backfilling a past application — the sheet stays sorted by date, inserting the row in the right place and renumbering No. automatically
- Editable review form before saving
- Writes directly to your Google Sheet
- **Update from Email** tab: paste a recruiter/company email and the AI matches it to an
  existing application, then appends a dated history entry (status change + details) to
  that row's Company Comments — nothing gets overwritten, so interview steps, follow-ups,
  and the eventual accept/reject all stay visible in one place
- **📦 Bulk Email Update** tab (optional, needs Gmail OAuth setup — see below): scans
  Gmail read-only for historical job-search emails, groups them per application, and
  proposes sheet updates for you to review and approve — nothing is written until you
  explicitly apply it

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get an OpenRouter API key — primary provider

1. Go to [openrouter.ai/keys](https://openrouter.ai/keys), sign in, and create a key — it starts with `sk-or-v1-...`
2. Add pay-as-you-go credit (OpenRouter is prepaid, not billed after the fact)
3. No model-specific activation needed — this app requests `openai/gpt-oss-120b` explicitly and OpenRouter routes it to whichever of its upstream providers is available for that model

### 2b. (Recommended) Get a Groq API key — automatic fallback

Groq is not required for the app to work, but without it an OpenRouter outage or
misconfiguration has nowhere to fall back to.

1. Go to [console.groq.com/keys](https://console.groq.com/keys)
2. Sign in and click **Create API Key**
3. Copy the key — it starts with `gsk_...`

### 3. Set up Google Sheets API

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable **Google Sheets API** (search in the API library)
4. Go to **IAM & Admin → Service Accounts** → Create service account
5. Give it any name (e.g. `job-tracker`), click Done
6. Click the service account → **Keys** tab → **Add Key → Create new key → JSON**
7. Save the downloaded file as `credentials.json` in this folder
8. **Share your Google Sheet** with the service account email (found in `credentials.json` as `client_email`) — give it **Editor** access

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env and add APP_PASSWORD, your OPENROUTER_API_KEY, and (recommended) GROQ_API_KEY
```

Your `.env` should look like:
```
APP_PASSWORD=a-strong-password-only-you-know
OPENROUTER_API_KEY=sk-or-v1-...
GROQ_API_KEY=gsk_...
GOOGLE_CREDENTIALS_FILE=credentials.json
```

`APP_PASSWORD` is required — see [Password gate](#password-gate) below; the app refuses
to run without it, on purpose. `GROQ_API_KEY` is optional — the app only reaches for it
if OpenRouter fails or isn't configured at all. Leaving both AI keys unset means AI
features simply won't work until you add at least one.

### 5. (Optional) Add a career profile for job matching

Place a Markdown file named `Mehdi_Mokhtari_Master_Career_Knowledge_Base.md` in this folder
with your background, skills, and experience. If present, the app compares each parsed job
against it and shows a Match Level (%) and Missing Skills. Without it, matching is skipped
and everything else works as normal.

### 6. Run

```bash
streamlit run app.py
```

Opens at `http://localhost:8501` — you'll hit a password prompt first, see [Password gate](#password-gate) below.

---

## Password gate

The app is gated by a single application password, stored as the `APP_PASSWORD`
secret/environment variable — never hardcoded, never a fallback. This matters more than
it used to: the app now also holds a Google Sheets service account and a persisted
Gmail OAuth refresh token, so the gate needed to stop being "just enough to deter a
stumbled-upon URL."

- **Set your own password.** Pick a strong, unique value and put it in `.env` locally
  or Streamlit Secrets when deployed (see below). There is no default — if
  `APP_PASSWORD` isn't set, the app shows a configuration error and refuses to run
  rather than falling back to anything.
- **The password is never in the URL, a cookie, or localStorage.** It's compared with
  `hmac.compare_digest()` and, once correct, kept only in
  `st.session_state["authenticated"]` for the current Streamlit session. There's
  nothing to bookmark or share by URL.
- **You'll be asked again** after the browser/tab's session ends, after the Streamlit
  process restarts, or after a redeploy. That's the intended tradeoff for a
  single-user personal tool — simple and safe rather than persistent and clever.
- **Logout**: a small **🚪 Logout** button in the sidebar clears the session and
  returns you to the login screen.

---

## AI provider & fallback

All three AI operations (parsing a job posting, matching it against your career profile,
and parsing an email) go through one shared function that always tries **OpenRouter**
(`openai/gpt-oss-120b`) first, and automatically falls back to **direct Groq**
(same model) if OpenRouter fails for any reason — not configured, timeout, rate limit, API
error, or an invalid/non-JSON response. Both providers receive the exact same prompt, so
results stay consistent regardless of which one actually answered. OpenRouter is requested
to serve `openai/gpt-oss-120b` specifically (not OpenRouter's auto-router, and not a free
variant) and routes that request among its own available upstream providers for that model.

- **Reasoning output**: `gpt-oss-120b` is a reasoning model — without telling it otherwise,
  it returns a large internal reasoning trace instead of the clean JSON object the prompts
  ask for (confirmed live: a 130K+ character non-JSON response). The OpenRouter call sets
  `extra_body={"reasoning": {"exclude": True}}`, OpenRouter's documented way to let the model
  reason internally but exclude that trace from the returned content. Direct Groq isn't
  affected by this — Groq's own hosting of the model apparently doesn't have the same
  default behavior via its standard chat completions API.
- **Privacy / provider routing**: OpenRouter documents a `provider.data_collection` field
  (`"allow"` | `"deny"`) to restrict routing to only providers that don't store your data,
  and a stricter `provider.zdr` (zero data retention) flag. Neither is applied by default in
  this app — reliability was prioritized over restricting the provider pool, since it's
  unclear how many of `gpt-oss-120b`'s upstream providers on OpenRouter would qualify. If you
  want this, add `"provider": {"data_collection": "deny"}` (or `{"zdr": true}` for the
  stricter option) to the `extra_body` in `_call_openrouter()` — test that OpenRouter still
  finds a route for the model before relying on it.
- **How to tell which provider handled a request**: nothing shows up when OpenRouter succeeds
  (the normal case). If Groq's fallback was used instead, you'll see a small
  "⚙️ OpenRouter was unavailable for this request — used Groq fallback" note under
  **Review & Edit** (Add Job tab) or **Review & Apply Update** (Update from Email tab).
- **If both fail**: you'll get a clear error naming both providers and why each one failed
  (e.g. "not configured" vs. an actual API error) — never your API keys.
- **If you only configure one provider**: that's fine. Configure just `OPENROUTER_API_KEY` to
  use OpenRouter only, or just `GROQ_API_KEY` to use Groq only (OpenRouter will simply fail
  closed and fall through every time). Configuring neither means AI features error out
  immediately with a setup message.

---

## Deploy to Streamlit Cloud (free)

1. Push this repo to GitHub (make sure `.env` and `credentials.json` are gitignored)
2. Go to [share.streamlit.io](https://share.streamlit.io) and connect your repo
3. In the app settings → **Secrets**, paste the contents of `.streamlit/secrets.toml.example` filled with your real values:

```toml
# Required — the app fails closed without this. Pick a strong, unique password; it
# never appears in the URL, a cookie, or localStorage.
APP_PASSWORD = "replace-with-a-strong-random-password"

OPENROUTER_API_KEY = "sk-or-v1-..."

# Optional but recommended — automatic fallback if OpenRouter fails or isn't configured
GROQ_API_KEY = "gsk_..."

# Optional — paste the full contents of your career profile .md file here to enable
# job matching on the cloud (locally the app reads the file directly, see step 5 above)
career_profile = """
# Your Career Knowledge Base
... paste full file content here ...
"""

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n"
client_email = "job-tracker@your-project.iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
```

> Tip: copy the values directly from your `credentials.json` file into the `[gcp_service_account]` block.

---

## 📦 Bulk Email Update (Gmail)

Optional. A separate tab that scans your Gmail (read-only) for historical job-search
emails and proposes sheet updates for you to review — for backfilling months of
applications without pasting them one at a time into **Update from Email**. It's
purely additive: everything else in the app works exactly the same whether or not you
set this up, and until you do, the tab just shows a short "not configured" message.

**How it works, in one paragraph:** you connect your Gmail account once (OAuth,
read-only), pick a date range and a Gmail label, and click **Scan Gmail**. Scanning
only *reads* — it never touches your Google Sheet, and any email sent *from* your own
address (e.g. a reply inside a labeled recruiter thread) is filtered out deterministically
before it ever reaches the AI, at no LLM cost. Each remaining email is run through the
same AI extraction and deterministic row-matching the single-email flow already uses,
routed to the **2025** archive tab or your current-year sheet based on the email's own
date — same logic as **Update from Email**, just applied at scan time. Emails that
resolve to the same tracked row are grouped into one proposal (current vs. proposed
Status/Contact/Comments, each proposed comment editable before you approve it) for you
to approve, edit, or skip. Ambiguous matches are never guessed — you're shown enough
detail on each candidate row (company, role, status, date applied) to pick the right
one yourself. Nothing is written to the sheet until you click **Apply Approved
Updates**, and only for the items you approved.

The setup below matches the current Google Auth Platform UI (Google has redesigned
this a few times — if your screens look different, the four sections referenced —
**Branding**, **Audience**, **Data access**, **Clients** — are the ones to look for).

### A. Enable the Gmail API

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and open the
   **same project** your `credentials.json` service account belongs to (or a new one —
   either works, Gmail OAuth is independent of the Sheets service account).
2. **APIs & Services → Library** → search **Gmail API** → **Enable**.

### B. Branding

Go to **Google Auth Platform → Branding**. For this personal, Testing-mode setup:

| Field | Value |
|---|---|
| App name | `Job Tracker` |
| User support email | your Google account |
| Application home page | **leave blank** |
| Privacy policy | leave blank |
| Terms of Service | leave blank |
| Authorized domains | **leave blank** |
| Developer contact information | your Google account |

**Important — leave the domain fields empty.** Don't put `streamlit.app` into
Authorized domains: Google rejects it, because `streamlit.app` is a shared public
suffix (many apps live under it), not a private domain this project owns. Don't put
`https://my-job-tracker.streamlit.app` into Application home page either — doing so
makes Google require its domain to be added under Authorized domains, which hits the
exact same rejection. For a personal Testing-mode app none of these fields are
required, so the simplest fix is to leave them all blank.

### C. Audience

Go to **Google Auth Platform → Audience**.

- **User type**: External
- **Publishing status**: **Testing** — do not publish
- **Test users**: add the Gmail account the tracker should read (your own)

While the app stays in Testing, only accounts listed as test users can authorize it —
that's the whole access boundary, and it's sufficient for a single-user tool.

### D. Data access

Go to **Google Auth Platform → Data access → Add or remove scopes**, and add exactly
one:

| Scope | User-facing description |
|---|---|
| `https://www.googleapis.com/auth/gmail.readonly` | View your email messages and settings |

Google currently lists `gmail.readonly` as a **restricted** Gmail scope — that's
expected, not a misconfiguration. Do **not** add `gmail.modify`, `gmail.send`,
`gmail.compose`, or similar — this app never sends, deletes, or modifies mail. In
particular, don't confuse `gmail.readonly` with
`gmail.addons.current.message.readonly` — that one is for building Gmail Add-ons and
is not what this tracker needs.

### E. Create the OAuth client

Go to **Google Auth Platform → Clients → Create client**.

| Field | Value |
|---|---|
| Application type | **Web application** |
| Name | `Job Tracker Streamlit` |
| Authorized JavaScript origins | leave empty |
| Authorized redirect URIs | `https://my-job-tracker.streamlit.app` |

For local development you can optionally also add `http://localhost:8501` as a second
redirect URI on the same client. Whichever URI you use, the app's own
`GOOGLE_GMAIL_REDIRECT_URI` secret must match it **exactly** — trailing slash and all.

After creating the client, Google shows a **Client ID** and **Client secret**. Copy
both now — neither belongs in Git; they only ever go into Streamlit Secrets or your
local `.env`.

### F. Add the secrets

Locally, in `.env`:

```
GOOGLE_GMAIL_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_GMAIL_CLIENT_SECRET=your-client-secret
GOOGLE_GMAIL_REDIRECT_URI=http://localhost:8501
```

On Streamlit Cloud, alongside the secrets from the deploy section above:

```toml
APP_PASSWORD = "replace-with-a-strong-random-password"

OPENROUTER_API_KEY = "sk-or-v1-..."
GROQ_API_KEY = "gsk_..."

GOOGLE_GMAIL_CLIENT_ID = "your-client-id.apps.googleusercontent.com"
GOOGLE_GMAIL_CLIENT_SECRET = "your-client-secret"
GOOGLE_GMAIL_REDIRECT_URI = "https://my-job-tracker.streamlit.app"

career_profile = """..."""

[gcp_service_account]
...
```

`GOOGLE_GMAIL_REDIRECT_URI` here must be `https://my-job-tracker.streamlit.app` — the
same URI registered on the client in step E. `APP_PASSWORD` and
`GOOGLE_GMAIL_CLIENT_SECRET` both go **only** into Streamlit Secrets / your local
environment — never into a file that gets committed to GitHub.

### G. First connection

1. Open Job Tracker and log in with your application password.
2. Open the **📦 Bulk Email Update** tab.
3. Click **Connect Gmail**.
4. Sign into the Gmail account you added as a test user.
5. Approve the read-only Gmail access Google's consent screen asks for.
6. You're redirected back to Job Tracker.
7. The tab should now show **Gmail: ✅ Connected**.

(Google's own consent-screen wording and any "unverified app" warning can vary by
account and by when you're reading this — if you see one, it's expected for a personal
Testing-mode app only you use; there isn't one exact screen to guarantee here.)

**If step 6 lands you back on the Job Tracker login screen instead of Connected:**
that's Streamlit Cloud's own session occasionally not surviving the trip out to
Google's consent screen and back — not a broken OAuth flow. Just log in again with
your application password; the app still has the OAuth code from the URL and finishes
connecting Gmail as soon as you're back in, no need to click Connect Gmail a second
time.

### H. Safe first test

Before trusting it with your full history, run a small, conservative test:

1. Select **Custom range** and pick roughly the last **7–14 days**.
2. Leave the Gmail label as `Jobsearch` (or set it to whatever label you actually use).
3. Click **Scan Gmail**.
4. Review the proposals in the review queue.
5. **Do not click Apply Approved Updates yet.**
6. Confirm your Google Sheet is unchanged — check its revision history (File → Version
   history) before and after the scan, or just glance at the rows you'd expect to be
   affected.

Once you're happy with what a small scan proposes, expand to larger periods (the full
**2025** archive, or **Jan–Jul 2026**) with more confidence.

**Scan Gmail is read-only with respect to application rows, always** — `scan_period()`
in `gmail_bulk.py` never calls the functions that write to Sheets. Only **Apply
Approved Updates** may write, and only for items you've explicitly marked **Approve**.

### Other things worth knowing

- **Own sent messages are skipped before the AI ever sees them** — the app looks up
  your own Gmail address (`users().getProfile`) once per scan and filters out anything
  sent from it, deterministically, with no LLM cost.
- **Ambiguous matches** always show enough detail (company, role, status, date applied)
  on every candidate row to pick correctly — never auto-selected.
- **Proposed comments are editable** in the review screen before you approve — your
  edited text is what gets written, not the AI's original wording; existing Company
  Comments are always shown alongside and are never erased.
- **2025 vs. current-year routing**: a scanned email dated in 2025 is matched and
  updated against the **2025** worksheet tab; anything else uses your current-year
  sheet — the same routing **Update from Email** already uses.
- **Applying a large batch is resilient to Google's rate limits**: writing many
  consolidated groups in one go can hit Sheets' write quota — each write that hits a
  rate-limit/transient error is retried automatically with backoff, and if a batch is
  ever interrupted partway through, clicking **Apply Approved Updates** again safely
  skips whatever was already successfully applied (checked against the Email Import
  Log) instead of writing duplicate comments or duplicate application rows.
- **Email Import Log**: a worksheet (auto-created, visible in your sheet tabs) records
  each Gmail message only *after* its update/creation is successfully applied — never
  merely for being scanned. Re-scanning the same period skips already-applied messages,
  so nothing gets duplicated and nothing gets re-billed to the AI.
- **Live progress during a scan**: a progress bar and status line show messages
  processed so far, running counts (parsed / skipped / failed), the current message's
  date/company/role/subject (never the email body), elapsed time, and — once enough
  messages have been processed — an approximate ETA. A large historical scan can still
  take a long time; you can now see it moving. A **🛑 Cancel scan** button is always
  available while a scan is running — the scan processes one message per screen update,
  so cancelling takes effect almost immediately rather than only after the whole range
  finishes. Whatever was already processed is still shown as review results.
- **Emails are consolidated into one application timeline**: multiple emails for the
  same tracked row are always one group, regardless of wording. Untracked emails are
  grouped by company+role only when a "thank you for applying"-style confirmation email
  anchors them — a later status update for the same company+role joins that group, but
  two genuinely separate applications to the same company+role (each with its own
  confirmation) stay as two groups. Anything uncertain stays its own item for you to
  review rather than being merged automatically. This consolidation works across a
  year boundary too — an application made in December that gets a status update the
  following January or February still consolidates into one group, homed in the
  correct archive sheet, instead of splitting across two years.
- **Low-value proposals are filtered out of the review queue**: a proposed status
  regression from Rejected back to Applied, a redundant Applied→Applied update with
  nothing new to add (unless it would fill in a currently-blank contact), and a
  brand-new application with no identifiable role are all skipped rather than shown —
  none of them are written anywhere, and a later re-scan surfaces them the same way,
  so nothing is lost, just decluttered.
- **Final proposed status is whichever event is chronologically latest** in a group's
  timeline — not a fixed hierarchy (e.g. Applied → Interview → Rejected proposes
  Rejected; Applied → Interview → Assessment proposes Assessment). The existing
  safeguard still applies: if the sheet already has a more recent update than this
  batch, you'll see the conflict warning instead of a silent downgrade.
- **Where the Gmail token is stored**: a **hidden worksheet tab** (`_gmail_oauth_token`)
  in the same spreadsheet, not local disk — Streamlit Cloud wipes local disk on every
  redeploy (which happens on every git push to this repo), so a local token file would
  force reconnecting almost every time you pushed a change. The service account that
  already reads/writes this whole spreadsheet is the same one that can read this hidden
  tab, so this doesn't expand what was already trusted with your data. The token is
  never logged or shown in the UI. Click **Disconnect** in the tab to clear it anytime.

### Known limitations

- Large scans page through Gmail's API sequentially (list + fetch per message, no batch
  requests yet) — hundreds of messages will take a while but will complete, with live
  progress shown throughout.
- The Gmail label filter matches Gmail's own `label:"..."` search syntax; if your label
  name differs, change the label field in the Bulk Email Update tab (it doesn't have to
  be `Jobsearch`, that's just the default).

---

## Google Sheet columns

| # | Column | Source |
|---|--------|--------|
| A | No. | Auto-increment |
| B | Date Applied | Current CET time |
| C | Company | Extracted from URL |
| D | Role | Extracted from URL |
| E | City | Extracted from URL |
| F | Language Req. | Extracted from URL |
| G | Key Skills Required | Extracted from URL |
| H | Contact Person | Extracted from URL |
| I | Job URL | Your input |
| J | Status | Default: Applied |
| K | Comments | Extracted from URL |
| L | CV Language | EN / DE toggle |
| M | Source | Auto-detected from URL, or your Source dropdown choice |
| N | Company Comments | Appended history from the Update from Email tab |
| O | Match Level | % fit vs. your career profile (if configured) |
| P | Missing Skills | Gaps vs. your career profile (if configured) |

Column M (Source) must exist in your sheet's header row before you start — add "Source" as
the header of column M if it isn't there yet. Columns N–P (Company Comments, Match Level,
Missing Skills) are created automatically on first run if they don't already exist.

---

## Tips

- **LinkedIn / auth-required sites**: use the "paste manually" option — copy the job description text from the page and paste it in
- **CV Language**: toggle between EN and DE before or after parsing; you can also change it in the review form
- **Status**: change from "Applied" in the review form if needed (Interview, Offer, etc.)
- **Source**: auto-detected from the job URL when possible; pick "Other" in the dropdown to enter a custom source (e.g. referral, career fair) — leave the text box blank to just record "Other"
- **Update from Email**: paste the email as-is (subject + body); the AI reads the email's own date, matches it to a job, and appends a new dated entry to Company Comments — it never overwrites earlier entries, so you get a full timeline per application. The AI only ever sees the email itself — never your job list — because it's reliably accurate at extracting the company, role, status, and dates from the email text alone; matching that extraction to a specific row is instead done locally and deterministically against your *full* job history (e.g. inferring the company from a sender domain like `@bbraun.com`, or catching a duplicate paste of an email you already added) — you'll see a "🔎 matched by company and role" note asking you to confirm it. If a company has only one tracked application, that's matched on company alone — an email's own wording of a role (say, from a subject line) is often the full official title rather than the shortened one stored when the job was added, so requiring both to line up there would fail for no reason. Once a company has more than one tracked application, both company **and** role need to match, so a different role at a company you've applied to before (or an old rejected attempt) doesn't get misfiled against the wrong row — role matching only strips a gender/diversity marker like "(m/w/d)", never other parenthetical content, since titles routinely use parens for a real differentiator too (e.g. "(Logistics - Customer, Time & Tracking)" distinguishing one open role from another very similarly named one at the same company). When several of a company's roles still share a common prefix, an exact title match is preferred over a partial one rather than treating them as equally ambiguous. (An earlier design asked the AI to also cite the row number directly from a list of all applications — dropped because citing an exact row out of 900+ candidates turned out to be unreliable even when the company/role extraction was correct, occasionally pointing at the wrong application with high stated confidence.)
- **Status confidence**: alongside the row-match confidence, the AI also scores how confident it is that the detected status (e.g. Rejected vs. Interview) correctly reflects the email's wording — explicit language like "we regret to inform you" or "we'd like to invite you to interview" gets high confidence, while vague/templated phrasing (e.g. "we'll keep your profile on file", "still under review") gets low confidence with an on-screen warning to double-check before applying
- **New application confirmations**: if the pasted email is an "application received" acknowledgment (e.g. "Eingangsbestätigung") for a job not yet in your sheet, the job selector defaults to "➕ Add as a new application" instead of an existing row — pre-filled with company, role, contact, and the email's own date/time, with a confidence score for how sure the AI is this is genuinely untracked. Adding it inserts the row in the correct chronological position and renumbers No. automatically, same as backdating in the Add Job tab. You can always override the dropdown either way — pick an existing row if it defaulted to "add new" incorrectly, or pick "add new" if it defaulted to the wrong existing row
- **Row formatting**: every row the app adds or updates gets formatted automatically — Date Applied is stored as a real right-aligned date; the bulleted columns (Key Skills, Comments, Company Comments, Missing Skills) are top-aligned with each bullet kept to a single line (long bullets get cut off rather than wrapping); and row height is sized to fit the bullets present, capped at 10 — beyond that the rest stays in the cell, just not shown until you click in
- **Pasting data directly into the sheet** (e.g. a raw LinkedIn Q&A export pasted straight into Comments) bypasses the app, so it won't get the formatting above automatically. Use the **🧹 Normalize formatting** button (top of the Add Job tab) afterward — it converts pipe-delimited text to bullets and reapplies consistent formatting across the whole sheet. Safe to run anytime; already-clean rows are left alone
