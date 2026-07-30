# Job Application Tracker

A Streamlit web app that extracts job details from any URL using AI and saves them to a Google Sheet.

**Features**
- Paste any job URL → AI (Claude) extracts company, role, city, skills, contact, and more
- Falls back to manual paste for sites that block scrapers (LinkedIn, etc.)
- CV Language toggle (EN / DE)
- Date Applied auto-set to current CET time
- Editable review form before saving
- Writes directly to your Google Sheet

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get an Anthropic API key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an API key
3. Copy it for the next step

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
# Edit .env and add your ANTHROPIC_API_KEY
```

Your `.env` should look like:
```
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_CREDENTIALS_FILE=credentials.json
```

### 5. Run

```bash
streamlit run app.py
```

Opens at `http://localhost:8501`

---

## Deploy to Streamlit Cloud (free)

1. Push this repo to GitHub (make sure `.env` and `credentials.json` are gitignored)
2. Go to [share.streamlit.io](https://share.streamlit.io) and connect your repo
3. In the app settings → **Secrets**, paste the contents of `.streamlit/secrets.toml.example` filled with your real values:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."

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

---

## Tips

- **LinkedIn / auth-required sites**: use the "paste manually" option — copy the job description text from the page and paste it in
- **CV Language**: toggle between EN and DE before or after parsing; you can also change it in the review form
- **Status**: change from "Applied" in the review form if needed (Interview, Offer, etc.)
