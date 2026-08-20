# Job Application Tracker

A Streamlit web app that extracts job details from any URL using AI and saves them to a Google Sheet.

**Features**
- Paste any job URL → AI (via Groq) extracts company, role, city, skills, contact, and more
- Falls back to manual paste for sites that block scrapers (LinkedIn, etc.)
- Optional job-fit matching against your career profile (Match Level + Missing Skills)
- Source dropdown auto-detected from the URL, with an "Other" option for custom sources
- CV Language toggle (EN / DE)
- Date Applied auto-set to current CET time
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

### 2. Get a Groq API key (free)

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
# Edit .env and add your GROQ_API_KEY
```

Your `.env` should look like:
```
GROQ_API_KEY=gsk_...
GOOGLE_CREDENTIALS_FILE=credentials.json
```

### 5. (Optional) Add a career profile for job matching

Place a Markdown file named `Mehdi_Mokhtari_Master_Career_Knowledge_Base.md` in this folder
with your background, skills, and experience. If present, the app compares each parsed job
against it and shows a Match Level (%) and Missing Skills. Without it, matching is skipped
and everything else works as normal.

### 6. Run

```bash
streamlit run app.py
```

Opens at `http://localhost:8501`

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

## Deploy to Streamlit Cloud (free)

1. Push this repo to GitHub (make sure `.env` and `credentials.json` are gitignored)
2. Go to [share.streamlit.io](https://share.streamlit.io) and connect your repo
3. In the app settings → **Secrets**, paste the contents of `.streamlit/secrets.toml.example` filled with your real values:

```toml
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
- **Update from Email**: paste the email as-is (subject + body); the AI reads the email's own date, matches it to a job, and appends a new dated entry to Company Comments — it never overwrites earlier entries, so you get a full timeline per application. To stay within Groq's per-minute token limit as your sheet grows, only the most recent applications are sent to the AI for matching — if it can't find a match (e.g. the email is about an older application, or the company was never added), just pick the right row yourself from the dropdown, which always lists everything. If the sender only identifies their company through the email domain (e.g. `@bbraun.com`, never spelling out "B. Braun" in the text), a local fallback matches on that after the AI call — you'll see a "🔎 found by matching company name" note asking you to confirm it instead of a full AI match
- **Row formatting**: every row the app adds or updates gets formatted automatically — Date Applied is stored as a real right-aligned date; the bulleted columns (Key Skills, Comments, Company Comments, Missing Skills) are top-aligned with each bullet kept to a single line (long bullets get cut off rather than wrapping); and row height is sized to fit the bullets present, capped at 10 — beyond that the rest stays in the cell, just not shown until you click in
- **Pasting data directly into the sheet** (e.g. a raw LinkedIn Q&A export pasted straight into Comments) bypasses the app, so it won't get the formatting above automatically. Use the **🧹 Normalize formatting** button (top of the Add Job tab) afterward — it converts pipe-delimited text to bullets and reapplies consistent formatting across the whole sheet. Safe to run anytime; already-clean rows are left alone
