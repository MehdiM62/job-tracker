import streamlit as st
import requests
from bs4 import BeautifulSoup
from groq import Groq
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pytz
import json
import os
from dotenv import load_dotenv

load_dotenv()

SHEET_ID = "1-9pSqdaqp8Sx_jhq1dq-KE0ExN4BdkHmpSXg-tzeEms"
CET = pytz.timezone("Europe/Berlin")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}


# ── Scraping ──────────────────────────────────────────────────────────────────

def fetch_url(url: str) -> str:
    """Fetch URL with browser headers, fall back to cloudscraper for protected sites."""
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=30, allow_redirects=True)
        r.raise_for_status()
        if len(r.text) > 500:
            return r.text
    except Exception:
        pass

    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "darwin", "mobile": False})
        r = scraper.get(url, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        raise RuntimeError(str(e))


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines() if line.strip()]
    return "\n".join(lines)[:14000]


# ── AI Parsing ────────────────────────────────────────────────────────────────

def get_groq_key() -> str:
    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        try:
            key = st.secrets.get("GROQ_API_KEY", "")
        except Exception:
            pass
    if not key:
        raise ValueError("GROQ_API_KEY not set. Add it to .env or .streamlit/secrets.toml")
    return key


def parse_job(text: str, url: str) -> dict:
    client = Groq(api_key=get_groq_key())

    prompt = f"""Extract structured information from this job posting.
Return ONLY a JSON object — no markdown, no explanation — with exactly these fields:

{{
  "company": "Company name",
  "role": "Exact job title",
  "city": "City, Country  (e.g. Berlin, Germany or Remote, Germany)",
  "language_req": "Language requirements  (e.g. German + English or English only)",
  "key_skills": "Required skills, one per line, each starting with • ",
  "contact_person": "Recruiter name, phone, email if found — otherwise Not specified",
  "comments": "Notable details (salary, remote %, job ID, deadline, benefits) — one per line starting with • "
}}

Job URL: {url}

Job text:
{text}"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(response.choices[0].message.content)


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_worksheet():
    try:
        info = dict(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    except Exception:
        path = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        if not os.path.exists(path):
            raise FileNotFoundError(
                "Google credentials not found.\n"
                "Place credentials.json in the app folder or set GOOGLE_CREDENTIALS_FILE in .env"
            )
        creds = Credentials.from_service_account_file(path, scopes=SCOPES)

    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).sheet1


def append_job(data: dict) -> int:
    ws = get_worksheet()
    all_rows = ws.get_all_values()
    data_rows = [r for r in all_rows[1:] if any(cell.strip() for cell in r)]
    next_no = len(data_rows) + 1
    now_cet = datetime.now(CET).strftime("%Y-%m-%d %H:%M")

    ws.append_row(
        [
            next_no,
            now_cet,
            data["company"],
            data["role"],
            data["city"],
            data["language_req"],
            data["key_skills"],
            data["contact_person"],
            data["url"],
            data["status"],
            data["comments"],
            data["cv_lang"],
        ],
        value_input_option="USER_ENTERED",
    )
    return next_no


# ── UI ────────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="Job Tracker", page_icon="💼", layout="centered")

    st.title("💼 Job Application Tracker")
    st.caption("Paste a job URL → AI extracts details → saved to your Google Sheet")

    # Increment this key after a successful save to force input widgets to reset
    if "input_key" not in st.session_state:
        st.session_state["input_key"] = 0
    k = st.session_state["input_key"]

    # ── Success message (shown at top, cleared after one render) ──────────────
    if "success_msg" in st.session_state:
        st.success(st.session_state.pop("success_msg"))
        st.balloons()

    # ── Input row ──────────────────────────────────────────────────────────────
    col_url, col_lang = st.columns([5, 1])
    with col_url:
        url = st.text_input("Job URL", placeholder="https://...", key=f"url_{k}")
    with col_lang:
        st.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
        cv_lang = st.radio(
            "CV", ["EN", "DE"],
            index=0 if st.session_state.get("cv_lang", "EN") == "EN" else 1,
            horizontal=True,
            label_visibility="collapsed",
            key=f"cv_{k}",
        )

    with st.expander("✏️ Or paste job description manually (for sites that block scraping)"):
        manual_text = st.text_area(
            "Paste job description text here",
            height=220,
            placeholder="Copy the full job description from the page and paste it here...",
            label_visibility="collapsed",
            key=f"manual_{k}",
        )

    fetch = st.button("🔍 Fetch & Parse", type="primary")

    # ── Fetch + Parse ──────────────────────────────────────────────────────────
    if fetch:
        text = manual_text.strip()

        if url.strip() and not text:
            with st.spinner("Fetching job page..."):
                try:
                    html = fetch_url(url.strip())
                    text = html_to_text(html)
                    if len(text) < 100:
                        st.warning("Page content looks too short — the site may block scrapers. Try pasting manually.")
                        st.stop()
                except RuntimeError as e:
                    st.error(f"Could not fetch the page: {e}")
                    st.info("Use the **paste manually** option above instead.")
                    st.stop()

        if not text:
            st.warning("Enter a URL or paste the job description.")
            st.stop()

        with st.spinner("Parsing with AI..."):
            try:
                parsed = parse_job(text, url.strip() or "")
                st.session_state["parsed"] = parsed
                st.session_state["job_url"] = url.strip()
                st.session_state["cv_lang"] = cv_lang
                st.success("Job details extracted — review and edit below.")
            except json.JSONDecodeError:
                st.error("AI returned unexpected output. Try again or simplify the pasted text.")
                st.stop()
            except Exception as e:
                st.error(f"Parsing failed: {e}")
                st.stop()

    # ── Editable Review Form ───────────────────────────────────────────────────
    if "parsed" in st.session_state:
        p = st.session_state["parsed"]

        st.divider()
        st.subheader("Review & Edit")

        now_cet = datetime.now(CET)
        st.info(f"📅 Date Applied (CET): **{now_cet.strftime('%Y-%m-%d %H:%M')}**")

        with st.form("job_form"):
            c1, c2 = st.columns(2)
            with c1:
                company  = st.text_input("Company",  value=p.get("company", ""))
                city     = st.text_input("City",     value=p.get("city", ""))
                lang_req = st.text_input("Language Requirement", value=p.get("language_req", ""))
            with c2:
                role    = st.text_input("Role", value=p.get("role", ""))
                status  = st.selectbox(
                    "Status",
                    ["Applied", "Interview", "Assessment", "Offer", "Rejected", "Withdrawn"],
                )
                saved_lang = st.session_state.get("cv_lang", "EN")
                cv_edit = st.radio(
                    "CV Language",
                    ["EN", "DE"],
                    index=0 if saved_lang == "EN" else 1,
                    horizontal=True,
                )

            contact    = st.text_input("Contact Person", value=p.get("contact_person", "Not specified"))
            key_skills = st.text_area("Key Skills Required", value=p.get("key_skills", ""), height=180)
            comments   = st.text_area("Comments",           value=p.get("comments", ""),    height=130)
            job_url    = st.text_input("Job URL",           value=st.session_state.get("job_url", ""))

            submitted = st.form_submit_button("✅ Add to Google Sheet", type="primary", use_container_width=True)

        if submitted:
            with st.spinner("Saving to Google Sheet..."):
                try:
                    row_no = append_job(
                        {
                            "company":        company,
                            "role":           role,
                            "city":           city,
                            "language_req":   lang_req,
                            "key_skills":     key_skills,
                            "contact_person": contact,
                            "url":            job_url,
                            "status":         status,
                            "comments":       comments,
                            "cv_lang":        cv_edit,
                        }
                    )
                    st.session_state["success_msg"] = f"🎉 Row #{row_no} added to Google Sheet!"
                    st.session_state["cv_lang"] = cv_edit  # preserve language choice
                    st.session_state["input_key"] += 1     # clears URL + manual text
                    del st.session_state["parsed"]
                    st.rerun()
                except FileNotFoundError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Failed to write to sheet: {e}")


if __name__ == "__main__":
    main()
