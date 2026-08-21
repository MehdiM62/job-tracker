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
import re
from dotenv import load_dotenv

load_dotenv()

APP_PASSWORD = "abc123"

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
OTHER_SOURCE = "Other"
SOURCE_OPTIONS = SOURCES + [OTHER_SOURCE]


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


JOBS_LIST_CHAR_BUDGET = 12000  # keeps the prompt well under Groq's per-minute token limit

def parse_email(email_text: str, jobs: list) -> dict:
    client = Groq(api_key=get_groq_key())
    # Sending every job can exceed the model's per-minute token budget once the sheet
    # grows large (each row costs real tokens). Email updates are almost always about
    # recent applications, so cap the candidate list to the most recent ones that fit
    # a safe character budget — the UI's manual selector still covers full history.
    lines, total_chars = [], 0
    for r in reversed(jobs):
        line = f"Row {r.get('No.', '')} | {r.get('Company', '')} | {r.get('Role', '')} | Status: {r.get('Status', '')}"
        if total_chars + len(line) > JOBS_LIST_CHAR_BUDGET:
            break
        lines.append(line)
        total_chars += len(line) + 1
    jobs_list = "\n".join(reversed(lines))
    truncated_note = (
        "\n(Only the most recent applications are listed above — if none match, set matched_row to null.)"
        if len(lines) < len(jobs) else ""
    )

    prompt = f"""You help track job applications. Analyze this email from a recruiter or company and match it to one of the applied jobs below.

If the company name isn't spelled out in the email body, infer it from the sender's
email domain (e.g. "haakon@bbraun.com" → "B. Braun") and match on that — recruiters
often only identify their company through the domain, not the visible text.

Applied jobs:
{jobs_list}{truncated_note}

Email:
{email_text}

Return ONLY a JSON object with these fields:
{{
  "matched_row": <row number as integer, or null if unclear>,
  "matched_company": "<company name from the email>",
  "matched_role": "<role/position from the email>",
  "email_date": "<date the email was sent/received, YYYY-MM-DD format — read it from the email's own date/header/signature, not today's date>",
  "new_status": "<updated status — one of: Applied, Interview, Assessment, Offer, Rejected, Withdrawn>",
  "company_comments": "<concise summary of what THIS email says, one point per line starting with •\\n — do not include the email date, it is recorded separately, e.g.: • Interview invite: 2026-08-14 10:00 via Teams\\n• Next round: technical interview\\n• Rejection reason: overqualified>",
  "confidence": "<high, medium, or low — confidence that matched_row is the CORRECT row in the applied jobs list above. If matched_row is null, this must be low, even if you're sure about the company/role from the email itself>"
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


CORP_SUFFIX_RE = re.compile(r"\b(gmbh|se|ag|inc|ltd|llc|kg|corp|corporation|plc|co)\b")

def normalize_company(name: str) -> str:
    name = name.lower()
    name = CORP_SUFFIX_RE.sub("", name)
    return re.sub(r"[^a-z0-9]", "", name)


def fuzzy_find_job(matched_company: str, jobs: list):
    """Deterministic backstop for when the AI can't confidently match a row (e.g. the
    company is only identifiable via the sender's email domain, which this model
    doesn't infer reliably every time). Normalizes company names and checks for a
    substring match against the FULL job history, not just the truncated AI candidate
    list — cheap and local, no extra API call. Requires a minimum length and a single
    unambiguous candidate, since short names (e.g. "SAP") risk false substring matches
    (e.g. "Sapient") and a wrong specific suggestion is worse than admitting no match."""
    target = normalize_company(matched_company or "")
    if len(target) < 4:
        return None
    candidates = [
        j.get("No.") for j in jobs
        if (c := normalize_company(str(j.get("Company", "")))) and (target in c or c in target)
    ]
    unique_rows = set(candidates)
    return candidates[0] if len(unique_rows) == 1 else None


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


ROW_BASE_PX = 5
ROW_LINE_PX = 16
ROW_MAX_LINES = 10

def format_row(ws, row_num: int, bullet_texts: list) -> None:
    """Keeps a data row visually consistent with the rest of the sheet: Date Applied
    stays a real right-aligned date, and the bulleted columns clip each bullet to a
    single line (long ones get cut off, not wrapped) with top alignment. Row height
    is sized to the tallest cell's bullet count, capped at ROW_MAX_LINES so a long
    history doesn't balloon the row — it takes exactly the space it needs, no more."""
    ws.batch_format([
        {
            "range": f"B{row_num}",
            "format": {
                "numberFormat": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm"},
                "horizontalAlignment": "RIGHT",
            },
        },
        {
            "range": f"A{row_num}:P{row_num}",
            "format": {"verticalAlignment": "TOP", "wrapStrategy": "CLIP"},
        },
    ])
    max_lines = max([t.count("\n") + 1 for t in bullet_texts if t and t.strip()], default=1)
    height = ROW_BASE_PX + min(max_lines, ROW_MAX_LINES) * ROW_LINE_PX
    ws.spreadsheet.batch_update({
        "requests": [{
            "updateDimensionProperties": {
                "range": {
                    "sheetId": ws.id, "dimension": "ROWS",
                    "startIndex": row_num - 1, "endIndex": row_num,
                },
                "properties": {"pixelSize": height},
                "fields": "pixelSize",
            }
        }]
    })


MULTI_LINE_COLS = ["Key Skills Required", "Comments", "Company Comments", "Missing Skills"]

def normalize_sheet_formatting(ws) -> dict:
    """One-shot cleanup for rows edited directly in the sheet (e.g. pasting a raw
    LinkedIn Q&A export straight into Comments), which bypasses format_row entirely.
    Converts any pipe-delimited text not already bulleted into bullet lines, then
    reapplies clip/top-align/height formatting to every data row. Safe to run
    repeatedly — already-clean rows are left as-is."""
    header = ws.row_values(1)
    comments_idx = header.index("Comments")
    multi_idx = [header.index(c) for c in MULTI_LINE_COLS]

    all_values = ws.get_all_values()
    data = all_values[1:]
    n_rows = len(data)
    if n_rows == 0:
        return {"rows_scanned": 0, "bullets_fixed": 0}

    value_updates = []
    for i, row in enumerate(data):
        raw = row[comments_idx] if comments_idx < len(row) else ""
        if " | " in raw and not raw.strip().startswith("•"):
            segments = [s.strip() for s in raw.split(" | ") if s.strip()]
            new_val = "\n".join("• " + s for s in segments)
            value_updates.append((i + 2, new_val))
            while len(row) <= comments_idx:
                row.append("")
            row[comments_idx] = new_val

    if value_updates:
        ws.batch_update(
            [{"range": f"K{row_num}", "values": [[val]]} for row_num, val in value_updates],
            value_input_option=gspread.utils.ValueInputOption.raw,
        )

    heights = []
    for row in data:
        max_lines = 1
        for idx in multi_idx:
            if idx < len(row) and row[idx].strip():
                max_lines = max(max_lines, row[idx].count("\n") + 1)
        heights.append(ROW_BASE_PX + min(max_lines, ROW_MAX_LINES) * ROW_LINE_PX)

    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": ws.id, "startRowIndex": 1, "endRowIndex": n_rows + 1,
                "startColumnIndex": 0, "endColumnIndex": 16,
            },
            "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "CLIP"}},
            "fields": "userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy",
        }
    }]
    start = 0
    for i in range(1, len(heights) + 1):
        if i == len(heights) or heights[i] != heights[start]:
            requests.append({
                "updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": start + 1, "endIndex": i + 1},
                    "properties": {"pixelSize": heights[start]},
                    "fields": "pixelSize",
                }
            })
            start = i

    for chunk_start in range(0, len(requests), 300):
        ws.spreadsheet.batch_update({"requests": requests[chunk_start:chunk_start + 300]})

    return {"rows_scanned": n_rows, "bullets_fixed": len(value_updates)}


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
    format_row(ws, next_no + 1, [data["key_skills"], data["comments"], data.get("missing_skills", "")])
    return next_no


def update_job_from_email(row_no: int, new_status: str, company_comments: str, email_date: str) -> bool:
    """Applies an email-derived update as a new dated history entry appended to
    Company Comments — prior entries (status changes, interview steps, etc.)
    are preserved rather than overwritten."""
    ws = get_worksheet()
    col_map = ensure_extra_cols(ws)
    all_values = ws.get_all_values()
    header = all_values[0]
    status_col = header.index("Status") + 1
    skills_col = header.index("Key Skills Required") + 1
    missing_col = header.index("Missing Skills") + 1
    cc_col = col_map["Company Comments"]

    for i, row in enumerate(all_values[1:], start=2):
        if row and str(row[0]).strip() == str(row_no):
            old_status = row[status_col - 1] if len(row) >= status_col else ""
            ws.update_cell(i, status_col, new_status)

            date_str = email_date.strip() if email_date and email_date.strip() else datetime.now(CET).strftime("%Y-%m-%d")
            status_line = (
                f"Status: {old_status} → {new_status}"
                if old_status and old_status != new_status
                else f"Status: {new_status}"
            )
            entry = f"📧 {date_str} | {status_line}\n{company_comments.strip()}".strip()

            existing = row[cc_col - 1] if len(row) >= cc_col else ""
            combined = (existing.strip() + "\n\n---\n\n" + entry).strip() if existing.strip() else entry
            ws.update_cell(i, cc_col, combined)

            skills = row[skills_col - 1] if len(row) >= skills_col else ""
            missing = row[missing_col - 1] if len(row) >= missing_col else ""
            format_row(ws, i, [skills, combined, missing])
            return True
    return False


# ── Auth ──────────────────────────────────────────────────────────────────────
# Hardcoded password gate — stops a stumbled-upon URL from touching real data.
# Not real security (the password travels in the URL), just a low-effort filter.

def is_authenticated() -> bool:
    if st.session_state.get("authenticated"):
        return True
    if st.query_params.get("pw") == APP_PASSWORD:
        st.session_state["authenticated"] = True
        return True
    return False


def login_gate() -> None:
    st.title("🔒 Job Tracker")
    with st.form("login_form"):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in", type="primary")
    if submitted:
        if pw == APP_PASSWORD:
            st.session_state["authenticated"] = True
            st.query_params["pw"] = APP_PASSWORD
            st.rerun()
        else:
            st.error("Incorrect password.")


# ── UI ────────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="Job Tracker", page_icon="💼", layout="centered")

    if not is_authenticated():
        login_gate()
        st.stop()

    st.title("💼 Job Application Tracker")

    if "input_key" not in st.session_state:
        st.session_state["input_key"] = 0
    if "email_key" not in st.session_state:
        st.session_state["email_key"] = 0

    if "success_msg" in st.session_state:
        st.success(st.session_state.pop("success_msg"))
        st.balloons()

    # A key makes the active tab a stateful widget tracked in session_state — without
    # one, Streamlit relies on fragile client-side-only memory to keep the same tab
    # selected across reruns, which intermittently resets to the first tab (e.g. right
    # after the "Parse Email" button triggers a rerun mid-spinner).
    tab_add, tab_email = st.tabs(["➕ Add Job", "📧 Update from Email"], key="active_tab")

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 1 — Add Job
    # ══════════════════════════════════════════════════════════════════════════
    with tab_add:
        with st.expander("🧹 Normalize formatting"):
            st.caption(
                "Run this after pasting data directly into the sheet (e.g. a raw LinkedIn "
                "Q&A export into Comments) — converts pipe-delimited text to bullets and "
                "reapplies consistent row formatting. Safe to run repeatedly."
            )
            if st.button("Normalize formatting"):
                with st.spinner("Scanning and reformatting..."):
                    try:
                        ws = get_worksheet()
                        result = normalize_sheet_formatting(ws)
                        st.success(
                            f"✅ Scanned {result['rows_scanned']} rows — "
                            f"fixed bullets on {result['bullets_fixed']} row(s)."
                        )
                    except Exception as e:
                        st.error(f"Normalization failed: {e}")

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
            if _default_source in SOURCE_OPTIONS:
                _source_idx = SOURCE_OPTIONS.index(_default_source)
                _default_other_text = ""
            else:
                # Previously used a custom "Other" source — reselect Other and prefill it
                _source_idx = SOURCE_OPTIONS.index(OTHER_SOURCE)
                _default_other_text = _default_source

            # Kept outside the form so choosing "Other" can reveal the custom text box immediately
            source_choice = st.selectbox(
                "Source", SOURCE_OPTIONS, index=_source_idx,
                help="Auto-detected from URL. Choose Other to enter a custom source.",
            )
            if source_choice == OTHER_SOURCE:
                custom_source = st.text_input(
                    "Custom source (optional)",
                    value=_default_other_text,
                    placeholder="e.g. Company website, referral, career fair",
                )
                source = custom_source.strip() or OTHER_SOURCE
            else:
                source = source_choice

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

            matched_row = r.get("matched_row")
            fuzzy_matched = False
            if matched_row is None:
                matched_row = fuzzy_find_job(r.get("matched_company", ""), jobs)
                fuzzy_matched = matched_row is not None

            if fuzzy_matched:
                st.info(
                    f"🔎 AI wasn't sure, but found Row {matched_row} by matching the company name "
                    f"**{r.get('matched_company', '')}** — please confirm it's correct below."
                )
            elif matched_row is None:
                st.warning(
                    f"⚠️ No matching application found for **{r.get('matched_company', 'this company')} — "
                    f"{r.get('matched_role', 'this role')}**. It may not be tracked yet, or falls outside "
                    f"the recent-applications window sent to the AI — select the correct row below, or "
                    f"add it as a new job first (Add Job tab) if it's missing entirely."
                )
            else:
                confidence = r.get("confidence", "low")
                conf_color = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(confidence, "🔴")
                st.caption(f"Match confidence: {conf_color} **{confidence.upper()}**")

            # Job selector — pre-select what AI found, let user override
            job_options = {
                f"Row {j.get('No.')} — {j.get('Company')} | {j.get('Role')} | {j.get('Status')}": j.get("No.")
                for j in jobs
            }
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
                        ok = update_job_from_email(selected_row_no, new_status, company_comments, email_date)
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
