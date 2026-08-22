# Job Application Tracker

A Streamlit web app that extracts job details from any URL using AI and saves them to a Google Sheet.

**Features**
- Paste any job URL → AI (Qwen3.5-Flash via QwenCloud — with an automatic Groq fallback) extracts company, role, city, skills, contact, and more
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

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get a Qwen API key — primary provider

1. Get an API key for `qwen3.5-flash` from [QwenCloud](https://www.qwencloud.com) (or the [Alibaba Cloud Model Studio console](https://www.alibabacloud.com/help/en/model-studio/first-api-call-to-qwen), which issues keys for the same backend) — it starts with `sk-...`
2. Make sure the account has completed whatever eligibility/billing step Alibaba requires before API calls succeed — a key that authenticates but returns `403 AccessDenied.Unpurchased` on every model means this step isn't done yet (check the console for a "Some Features Restricted" banner or similar) — this is an account-level gate, not something this app's code can work around
3. This app calls the **Singapore/International** endpoint (`dashscope-intl.aliyuncs.com`) — see the note in [AI provider & fallback](#ai-provider--fallback) below if you specifically need EU/Frankfurt data residency instead, which requires a different key and a small code change (a Frankfurt-region Model Studio workspace, not a QwenCloud key)

### 2b. (Recommended) Get a Groq API key — automatic fallback

Groq is not required for the app to work, but without it a Qwen outage or misconfiguration
has nowhere to fall back to.

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
# Edit .env and add your DASHSCOPE_API_KEY and (recommended) GROQ_API_KEY
```

Your `.env` should look like:
```
DASHSCOPE_API_KEY=sk-...
GROQ_API_KEY=gsk_...
GOOGLE_CREDENTIALS_FILE=credentials.json
```

`GROQ_API_KEY` is optional — the app only reaches for it if Qwen fails or isn't configured
at all. Leaving both unset means AI features simply won't work until you add at least one.

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

The app is gated by a single hardcoded password (`APP_PASSWORD` in `app.py`, default
`abc123`) — just enough to stop someone who stumbles on the URL from touching your data.
It's not real security: change `APP_PASSWORD` before deploying anywhere semi-public.

After logging in, the password is appended to the URL as `?pw=...` — reloading or
revisiting that exact URL skips the login prompt again, so bookmark it once and that
browser/tab stays "logged in" indefinitely. Visiting the bare URL (or sharing it without
the `?pw=...` part) asks for the password again.

---

## AI provider & fallback

All three AI operations (parsing a job posting, matching it against your career profile,
and parsing an email) go through one shared function that always tries **Qwen3.5-Flash**
first, and automatically falls back to **Groq (GPT-OSS-120B)** if Qwen fails for any reason
— not configured, timeout, rate limit, API error, or an invalid/non-JSON response. Both
providers receive the exact same prompt, so results stay consistent regardless of which one
actually answered.

- **Region / data residency**: Qwen calls go to `dashscope-intl.aliyuncs.com`, Alibaba
  Cloud's **Singapore/International** region — not the EU. An earlier version of this app
  used a Frankfurt-specific endpoint (a workspace-scoped `eu-central-1.maas.aliyuncs.com`
  URL, requiring a `DASHSCOPE_WORKSPACE_ID`), which is the correct setup if you need EU-only
  processing — but that requires an API key issued specifically from a Frankfurt-region
  Model Studio workspace (not a QwenCloud key, and not interchangeable with one — Alibaba
  keys are region-locked). If that matters for your use case, get a Frankfurt key and revert
  `QWEN_BASE_URL`/`_call_qwen()` in `app.py` to build the workspace-scoped URL instead (see
  git history for the previous implementation).
- **How to tell which provider handled a request**: nothing shows up when Qwen succeeds
  (the normal case). If Groq's fallback was used instead, you'll see a small
  "⚙️ Qwen was unavailable for this request — used Groq fallback" note under **Review & Edit**
  (Add Job tab) or **Review & Apply Update** (Update from Email tab).
- **If both fail**: you'll get a clear error naming both providers and why each one failed
  (e.g. "not configured" vs. an actual API error) — never your API keys.
- **If you only configure one provider**: that's fine. Configure just `DASHSCOPE_API_KEY` to
  use Qwen only, or just `GROQ_API_KEY` to use Groq only (Qwen will simply fail closed and
  fall through every time). Configuring neither means AI features error out immediately with
  a setup message.

---

## Deploy to Streamlit Cloud (free)

1. Push this repo to GitHub (make sure `.env` and `credentials.json` are gitignored)
2. Go to [share.streamlit.io](https://share.streamlit.io) and connect your repo
3. In the app settings → **Secrets**, paste the contents of `.streamlit/secrets.toml.example` filled with your real values:

```toml
DASHSCOPE_API_KEY = "sk-..."

# Optional but recommended — automatic fallback if Qwen fails or isn't configured
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
- **Update from Email**: paste the email as-is (subject + body); the AI reads the email's own date, matches it to a job, and appends a new dated entry to Company Comments — it never overwrites earlier entries, so you get a full timeline per application. To stay within the Groq fallback's per-minute token limit as your sheet grows (the same prompt is sent to both providers, so this budget is sized for the more conservative one), only the most recent applications are sent to the AI for matching — if it can't find a match within that window (e.g. the email is about an older application), a local fallback then checks your *full* job history for a row whose company **and role** both match closely (e.g. inferring the company from a sender domain like `@bbraun.com`, or catching a duplicate paste of an email you already added) — you'll see a "🔎 found by matching company name" note asking you to confirm it instead of a full AI match. Matching requires both company and role together specifically so a second, different application to a company you've already applied to before (a different role, or a past rejected attempt) doesn't get misfiled against that unrelated row
- **New application confirmations**: if the pasted email is an "application received" acknowledgment (e.g. "Eingangsbestätigung") for a job not yet in your sheet, the job selector defaults to "➕ Add as a new application" instead of an existing row — pre-filled with company, role, contact, and the email's own date/time, with a confidence score for how sure the AI is this is genuinely untracked. Adding it inserts the row in the correct chronological position and renumbers No. automatically, same as backdating in the Add Job tab. You can always override the dropdown either way — pick an existing row if it defaulted to "add new" incorrectly, or pick "add new" if it defaulted to the wrong existing row
- **Row formatting**: every row the app adds or updates gets formatted automatically — Date Applied is stored as a real right-aligned date; the bulleted columns (Key Skills, Comments, Company Comments, Missing Skills) are top-aligned with each bullet kept to a single line (long bullets get cut off rather than wrapping); and row height is sized to fit the bullets present, capped at 10 — beyond that the rest stays in the cell, just not shown until you click in
- **Pasting data directly into the sheet** (e.g. a raw LinkedIn Q&A export pasted straight into Comments) bypasses the app, so it won't get the formatting above automatically. Use the **🧹 Normalize formatting** button (top of the Add Job tab) afterward — it converts pipe-delimited text to bullets and reapplies consistent formatting across the whole sheet. Safe to run anytime; already-clean rows are left alone
