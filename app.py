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
STATUSES = ["Applied", "Interview", "Assessment", "Offer", "Rejected", "Withdrawn"]

# Domain → display name (order matters; first match wins)
SOURCE_DOMAINS = {
    "linkedin.com":       "LinkedIn",
    "stepstone.de":       "StepStone",
    "xing.com":           "XING",
    "glassdoor.com":      "Glassdoor",
    "it-jobs.de":         "IT-Jobs",
    "indeed.":            "Indeed",
    "get-in-it.de":       "get-in-it",
    "monster.de":         "Monster",
    "jobworld.de":        "jobworld",
    "talent-berlin.de":   "Talent Berlin",
    "englishjobs.de":     "English Jobs",
    "instaffo.com":       "Instaffo",
    "whybrilliant.com":   "whybrilliant",
    "arbeitsagentur.de":  "Arbeitsagentur",
}
# Deduplicated ordered list for the dropdown
SOURCES = list(dict.fromkeys(SOURCE_DOMAINS.values()))


def detect_source(url: str) -> str | None:
    url_lower = url.lower()
    for domain, name in SOURCE_DOMAINS.items():
        if domain in url_lower:
            return name
    return None

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


# ── AI ────────────────────────────────────────────────────────────────────────

PROFILE_PATH = os.path.join(os.path.dirname(__file__), "Mehdi_Mokhtari_Master_Career_Knowledge_Base.md")

def load_career_profile() -> str:
    """Load career profile from local file (dev) or Streamlit secrets (cloud)."""
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, encoding="utf-8") as f:
            return f.read()
    try:
        # Top-level key (correct position — above any [section] header)
        profile = st.secrets.get("career_profile", "")
        if profile:
            return profile
        # Fallback: mistakenly placed inside [gcp_service_account]
        profile = st.secrets.get("gcp_service_account", {}).get("career_profile", "")
        return profile
    except Exception:
        return ""


def format_bullets(text) -> str:
    """Guarantee each • bullet point is on its own line."""
    if isinstance(text, list):
        items = [str(i).strip().lstrip("•").strip() for i in text if str(i).strip()]
        return "\n".join("• " + i for i in items)
    if not text or not isinstance(text, str):
        return text or ""
    if "•" not in text:
        return text
    parts = [p.strip() for p in text.split("•") if p.strip()]
    return "\n".join("• " + p for p in parts)


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
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    result = json.loads(response.choices[0].message.content)
    result["key_skills"] = format_bullets(result.get("key_skills", ""))
    result["comments"]   = format_bullets(result.get("comments", ""))
    return result


def parse_email(email_text: str, jobs: list) -> dict:
    client = Groq(api_key=get_groq_key())
    jobs_list = "\n".join([
        f"Row {r.get('No.','')} | {r.get('Company','')} | {r.get('Role','')} | Status: {r.get('Status','')}"
        for r in jobs
    ])
    prompt = f"""You help track job applications. Analyze this email from a recruiter or company and match it to one of the applied jobs below.

Applied jobs:
{jobs_list}

Email:
{email_text}

Return ONLY a JSON object with these fields:
{{
  "matched_row": <row number as integer, or null if unclear>,
  "matched_company": "<company name from the email>",
  "matched_role": "<role/position from the email>",
  "email_date": "<date the email was sent, YYYY-MM-DD format>",
  "new_status": "<updated status — one of: Applied, Interview, Assessment, Offer, Rejected, Withdrawn>",
  "company_comments": "<concise summary where EACH point is on its own line starting with •\\n, e.g.: • Email date: 2026-08-07\\n• Interview: 2026-08-14 10:00 via Teams\\n• Rejection reason: overqualified>",
  "confidence": "<high, medium, or low — how confident you are about which job this email refers to>"
}}"""

    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    result = json.loads(response.choices[0].message.content)
    result["company_comments"] = format_bullets(result.get("company_comments", ""))
    return result


def match_job(parsed: dict, profile: str) -> dict:
    """Compare parsed job against the career profile. Returns match_level (0-100),
    match_summary, and missing_skills."""
    client = Groq(api_key=get_groq_key())

    job_snapshot = (
        f"Role: {parsed.get('role', '')}\n"
        f"Company: {parsed.get('company', '')}\n"
        f"Language requirement: {parsed.get('language_req', '')}\n"
        f"Key skills required:\n{parsed.get('key_skills', '')}\n"
        f"Comments / requirements:\n{parsed.get('comments', '')}"
    )

    prompt = f"""You are a career advisor performing a job-fit analysis.

CANDIDATE PROFILE:
{profile[:7000]}

JOB POSTING:
{job_snapshot}

Analyse how well the candidate fits this job and return ONLY a JSON object:
{{
  "match_level": <integer 0–100, where 100 = perfect fit>,
  "match_summary": "<one concise sentence explaining the score>",
  "missing_skills": "<skills or requirements from the job the candidate clearly lacks — one per line starting with •. Write None if there are no gaps.>"
}}"""

    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    result = json.loads(response.choices[0].message.content)
    result["missing_skills"] = format_bullets(result.get("missing_skills", ""))
    return result


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


EXTRA_COLS = ["Company Comments", "Match Level", "Missing Skills"]

def ensure_extra_cols(ws) -> dict:
    """Ensures Company Comments, Match Level, and Missing Skills columns exist.
    Returns dict of column_name → 1-based index."""
    header = ws.row_values(1)
    indices = {}
    next_col = len([h for h in header if h.strip()]) + 1

    for name in EXTRA_COLS:
        if name in header:
            indices[name] = header.index(name) + 1
        else:
            ws.update_cell(1, next_col, name)
            indices[name] = next_col
            next_col += 1
            header.append(name)  # keep header in sync for subsequent lookups

    return indices


def get_all_jobs(ws) -> list:
    return ws.get_all_records()


def find_duplicate(ws, url: str, company: str, role: str) -> dict | None:
    for rec in get_all_jobs(ws):
        if url and rec.get("Job URL", "").strip() == url.strip():
            return rec
        if (company and role
                and rec.get("Company", "").strip().lower() == company.strip().lower()
                and rec.get("Role", "").strip().lower() == role.strip().lower()):
            return rec
    return None


def append_job(data: dict) -> int:
    ws = get_worksheet()
    ensure_extra_cols(ws)
    all_rows = ws.get_all_values()
    data_rows = [r for r in all_rows[1:] if any(cell.strip() for cell in r)]
    next_no = len(data_rows) + 1
    now_cet = datetime.now(CET).strftime("%Y-%m-%d %H:%M")
    ml = data.get("match_level", "")
    match_display = f"{ml}%" if isinstance(ml, int) else str(ml)
    ws.append_row(
        [
            next_no, now_cet,
            data["company"], data["role"], data["city"],
            data["language_req"], data["key_skills"], data["contact_person"],
            data["url"], data["status"], data["comments"], data["cv_lang"],
            data.get("source", ""),          # M — Source
            "",                              # N — Company Comments
            match_display,                   # O — Match Level
            data.get("missing_skills", ""),  # P — Missing Skills
        ],
        value_input_option="USER_ENTERED",
    )
    return next_no


def update_job_from_email(row_no: int, new_status: str, company_comments: str) -> bool:
    ws = get_worksheet()
    col_map = ensure_extra_cols(ws)
    all_values = ws.get_all_values()
    header = all_values[0]
    status_col = header.index("Status") + 1
    cc_col = col_map["Company Comments"]

    for i, row in enumerate(all_values[1:], start=2):
        if row and str(row[0]).strip() == str(row_no):
            ws.update_cell(i, status_col, new_status)
            existing = row[cc_col - 1] if len(row) >= cc_col else ""
            combined = (existing.strip() + "\n\n" + company_comments).strip() if existing.strip() else company_comments
            ws.update_cell(i, cc_col, combined)
            return True
    return False


# ── UI ────────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="Job Tracker", page_icon="💼", layout="centered")
    st.title("💼 Job Application Tracker")

    if "input_key" not in st.session_state:
        st.session_state["input_key"] = 0
    if "email_key" not in st.session_state:
        st.session_state["email_key"] = 0

    if "success_msg" in st.session_state:
        st.success(st.session_state.pop("success_msg"))
        st.balloons()

    tab_add, tab_email = st.tabs(["➕ Add Job", "📧 Update from Email"])

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 1 — Add Job
    # ══════════════════════════════════════════════════════════════════════════
    with tab_add:
        k = st.session_state["input_key"]

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

        if fetch:
            text = manual_text.strip()
            if url.strip() and not text:
                with st.spinner("Fetching job page..."):
                    try:
                        html = fetch_url(url.strip())
                        text = html_to_text(html)
                        if len(text) < 100:
                            st.warning("Page content looks too short — try pasting manually.")
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
                    st.session_state.pop("duplicate", None)
                    st.session_state.pop("match_result", None)
                except json.JSONDecodeError:
                    st.error("AI returned unexpected output. Try again.")
                    st.stop()
                except Exception as e:
                    st.error(f"Parsing failed: {e}")
                    st.stop()

            profile = load_career_profile()
            st.session_state["profile_loaded"] = bool(profile)
            if profile:
                with st.spinner("Matching against your profile..."):
                    try:
                        match = match_job(parsed, profile)
                        st.session_state["match_result"] = match
                    except Exception as e:
                        st.session_state["match_error"] = str(e)

            # Duplicate check
            with st.spinner("Checking for duplicates..."):
                try:
                    ws = get_worksheet()
                    dup = find_duplicate(
                        ws,
                        url.strip(),
                        parsed.get("company", ""),
                        parsed.get("role", ""),
                    )
                    if dup:
                        st.session_state["duplicate"] = dup
                except Exception:
                    pass  # Don't block on duplicate check failure

        # ── Review form ───────────────────────────────────────────────────────
        if "parsed" in st.session_state:
            p = st.session_state["parsed"]
            dup = st.session_state.get("duplicate")

            st.divider()

            if dup:
                st.warning(
                    f"⚠️ **Possible duplicate** — you may have already applied to "
                    f"**{dup.get('Company')}** for **{dup.get('Role')}** "
                    f"(row #{dup.get('No.')}, applied {dup.get('Date Applied')}, "
                    f"status: {dup.get('Status')})."
                )

            st.subheader("Review & Edit")

            # ── Match display ─────────────────────────────────────────────────
            match = st.session_state.get("match_result", {})
            if match:
                ml = int(match.get("match_level", 0))
                if ml >= 75:
                    bar_color, bg, label = "#22c55e", "#14532d", "Strong Match"
                elif ml >= 45:
                    bar_color, bg, label = "#eab308", "#422006", "Moderate Match"
                else:
                    bar_color, bg, label = "#ef4444", "#450a0a", "Weak Match"

                st.markdown(f"""
<div style="background:{bg}; border-left:5px solid {bar_color};
     padding:18px 20px; border-radius:10px; margin-bottom:12px;">
  <div style="display:flex; align-items:center; gap:20px;">
    <div style="font-size:2.8em; font-weight:900; color:{bar_color}; line-height:1;">{ml}%</div>
    <div>
      <div style="font-size:1.15em; font-weight:700; color:{bar_color};">{label}</div>
      <div style="font-size:0.9em; color:#d1d5db; margin-top:4px;">{match.get("match_summary","")}</div>
    </div>
  </div>
  <div style="margin-top:12px; background:#ffffff18; border-radius:6px; height:10px; overflow:hidden;">
    <div style="width:{ml}%; background:{bar_color}; height:100%; border-radius:6px;"></div>
  </div>
</div>
""", unsafe_allow_html=True)
            elif st.session_state.get("profile_loaded") is False:
                st.info(
                    "💡 **Job matching not available** — career profile not found. "
                    "Add `career_profile` to your Streamlit secrets to enable matching."
                )
            elif st.session_state.get("match_error"):
                st.warning(f"⚠️ Matching failed: {st.session_state['match_error']}")

            st.info(f"📅 Date Applied (CET): **{datetime.now(CET).strftime('%Y-%m-%d %H:%M')}**")

            # Detect source from URL; fall back to last used source
            _job_url = st.session_state.get("job_url", "")
            _detected = detect_source(_job_url)
            _last = st.session_state.get("last_source", SOURCES[0])
            _default_source = _detected or _last
            _source_idx = SOURCES.index(_default_source) if _default_source in SOURCES else 0

            with st.form("job_form"):
                c1, c2 = st.columns(2)
                with c1:
                    company  = st.text_input("Company",  value=p.get("company", ""))
                    city     = st.text_input("City",     value=p.get("city", ""))
                    lang_req = st.text_input("Language Requirement", value=p.get("language_req", ""))
                with c2:
                    role   = st.text_input("Role", value=p.get("role", ""))
                    status = st.selectbox("Status", STATUSES)
                    cv_edit = st.radio(
                        "CV Language", ["EN", "DE"],
                        index=0 if st.session_state.get("cv_lang", "EN") == "EN" else 1,
                        horizontal=True,
                    )

                source     = st.selectbox("Source", SOURCES, index=_source_idx,
                                          help="Auto-detected from URL. Change if needed.")
                contact    = st.text_input("Contact Person", value=p.get("contact_person", "Not specified"))
                key_skills = st.text_area("Key Skills Required", value=p.get("key_skills", ""), height=180)
                comments   = st.text_area("Comments", value=p.get("comments", ""), height=130)
                missing_skills = st.text_area(
                    "Missing Skills (gaps vs your profile)",
                    value=match.get("missing_skills", "") if match else "",
                    height=120,
                )
                job_url    = st.text_input("Job URL", value=st.session_state.get("job_url", ""))

                if dup:
                    proceed = st.checkbox("I know — add anyway")
                    submitted = st.form_submit_button(
                        "✅ Add to Google Sheet", type="primary", use_container_width=True,
                        disabled=not proceed,
                    )
                else:
                    submitted = st.form_submit_button(
                        "✅ Add to Google Sheet", type="primary", use_container_width=True,
                    )

            if submitted:
                with st.spinner("Saving to Google Sheet..."):
                    try:
                        row_no = append_job({
                            "company": company, "role": role, "city": city,
                            "language_req": lang_req, "key_skills": key_skills,
                            "contact_person": contact, "url": job_url,
                            "status": status, "comments": comments,
                            "cv_lang": cv_edit, "source": source,
                            "match_level": match.get("match_level", "") if match else "",
                            "missing_skills": missing_skills,
                        })
                        st.session_state["success_msg"] = f"🎉 Row #{row_no} added to Google Sheet!"
                        st.session_state["cv_lang"] = cv_edit
                        st.session_state["last_source"] = source
                        st.session_state["input_key"] += 1
                        st.session_state.pop("parsed", None)
                        st.session_state.pop("duplicate", None)
                        st.rerun()
                    except FileNotFoundError as e:
                        st.error(str(e))
                    except Exception as e:
                        st.error(f"Failed to write to sheet: {e}")

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 2 — Update from Email
    # ══════════════════════════════════════════════════════════════════════════
    with tab_email:
        st.subheader("Update application status from an email")
        st.caption("Paste an email you received from a recruiter or company — the AI will identify the job and extract key information.")

        ek = st.session_state["email_key"]
        email_text = st.text_area(
            "Email content",
            height=280,
            placeholder="Paste the full email here (subject + body)...",
            label_visibility="collapsed",
            key=f"email_{ek}",
        )

        parse_email_btn = st.button("🔍 Parse Email", type="primary", key="parse_email_btn")

        if parse_email_btn:
            if not email_text.strip():
                st.warning("Paste an email first.")
                st.stop()

            with st.spinner("Loading your applications..."):
                try:
                    ws = get_worksheet()
                    jobs = get_all_jobs(ws)
                    if not jobs:
                        st.error("No job applications found in the sheet yet.")
                        st.stop()
                except Exception as e:
                    st.error(f"Could not load sheet: {e}")
                    st.stop()

            with st.spinner("Parsing email with AI..."):
                try:
                    result = parse_email(email_text.strip(), jobs)
                    st.session_state["email_parsed"] = result
                    st.session_state["email_jobs"] = jobs
                except Exception as e:
                    st.error(f"Parsing failed: {e}")
                    st.stop()

        if "email_parsed" in st.session_state:
            r = st.session_state["email_parsed"]
            jobs = st.session_state.get("email_jobs", [])

            st.divider()
            st.subheader("Review & Apply Update")

            confidence = r.get("confidence", "low")
            conf_color = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(confidence, "🔴")
            st.caption(f"Match confidence: {conf_color} **{confidence.upper()}**")

            # Job selector — pre-select what AI found, let user override
            job_options = {
                f"Row {j.get('No.')} — {j.get('Company')} | {j.get('Role')} | {j.get('Status')}": j.get("No.")
                for j in jobs
            }
            matched_row = r.get("matched_row")
            default_idx = 0
            for i, no in enumerate(job_options.values()):
                if no == matched_row:
                    default_idx = i
                    break

            selected_label = st.selectbox(
                "Which job does this email refer to?",
                list(job_options.keys()),
                index=default_idx,
            )
            selected_row_no = job_options[selected_label]

            with st.form("email_form"):
                c1, c2 = st.columns(2)
                with c1:
                    email_date = st.text_input("Email date", value=r.get("email_date", ""))
                with c2:
                    new_status = st.selectbox(
                        "New status",
                        STATUSES,
                        index=STATUSES.index(r.get("new_status", "Applied"))
                        if r.get("new_status") in STATUSES else 0,
                    )

                company_comments = st.text_area(
                    "Company Comments (will be appended to sheet)",
                    value=r.get("company_comments", ""),
                    height=200,
                )

                apply_btn = st.form_submit_button("✅ Update Sheet", type="primary", use_container_width=True)

            if apply_btn:
                with st.spinner("Updating sheet..."):
                    try:
                        # Prepend email date to comments if not already there
                        comment_with_date = company_comments.strip()
                        if email_date and email_date not in comment_with_date:
                            comment_with_date = f"📧 {email_date}\n{comment_with_date}"

                        ok = update_job_from_email(selected_row_no, new_status, comment_with_date)
                        if ok:
                            st.session_state["success_msg"] = (
                                f"✅ Row #{selected_row_no} updated — Status: {new_status}"
                            )
                            st.session_state.pop("email_parsed", None)
                            st.session_state.pop("email_jobs", None)
                            st.session_state["email_key"] += 1  # clears email text area
                            st.rerun()
                        else:
                            st.error(f"Row #{selected_row_no} not found in the sheet.")
                    except Exception as e:
                        st.error(f"Failed to update sheet: {e}")


if __name__ == "__main__":
    main()
