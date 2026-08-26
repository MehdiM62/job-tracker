import streamlit as st
import requests
from bs4 import BeautifulSoup
from groq import Groq
from openai import OpenAI
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


def _get_secret(name: str) -> str:
    """Checks os.environ first (local .env), then st.secrets (Streamlit Cloud)."""
    value = os.getenv(name, "")
    if not value:
        try:
            value = st.secrets.get(name, "")
        except Exception:
            pass
    return value


# ── LLM provider abstraction ────────────────────────────────────────────────
# OpenRouter (routing to openai/gpt-oss-120b among its available upstream
# providers) is the primary provider for all AI calls; direct Groq (same model)
# is an automatic fallback if OpenRouter fails for any reason (not configured,
# timeout, rate limit, API error, or a malformed/non-JSON response). Both
# providers get the exact same prompt — call_llm() is the single place that
# knows about providers, so parse_job/parse_email/match_job stay
# provider-agnostic and their JSON schemas are untouched.
#
# Previously used Qwen3.5-Flash (via Alibaba Cloud / QwenCloud) as primary, but
# account-level access restrictions there ("AccessDenied.Unpurchased" on every
# model, regardless of region or key) made it unworkable — see git history for
# that implementation if revisiting Qwen later.

OPENROUTER_MODEL = "openai/gpt-oss-120b"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GROQ_MODEL = "openai/gpt-oss-120b"


def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


def _call_openrouter(prompt: str) -> dict:
    api_key = _get_secret("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OpenRouter not configured (OPENROUTER_API_KEY missing)")

    client = OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)
    try:
        response = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
            extra_headers={
                # Optional site-attribution headers OpenRouter documents for rankings —
                # non-sensitive, requests work fine without them if this ever changes.
                "HTTP-Referer": "https://github.com/MehdiM62/job-tracker",
                "X-Title": "Job Application Tracker",
            },
            # gpt-oss-120b is a reasoning model; without this it returns huge reasoning
            # traces instead of the requested JSON object (confirmed via live testing —
            # a 130K+ character non-JSON response). exclude=True keeps the model
            # reasoning internally but strips that content from what's returned.
            extra_body={"reasoning": {"exclude": True}},
        )
        content = response.choices[0].message.content
    except Exception as e:
        raise RuntimeError(_redact(str(e), api_key)) from e

    result = json.loads(content)  # JSONDecodeError also triggers call_llm()'s fallback
    if not isinstance(result, dict):
        raise ValueError("OpenRouter response was not a JSON object")
    return result


def _call_groq(prompt: str) -> dict:
    api_key = _get_secret("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Groq not configured (GROQ_API_KEY missing)")

    client = Groq(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = response.choices[0].message.content
    except Exception as e:
        raise RuntimeError(_redact(str(e), api_key)) from e

    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Groq response was not a JSON object")
    return result


def call_llm(prompt: str) -> dict:
    """Sends prompt to OpenRouter first, falling back to direct Groq if OpenRouter
    fails for any reason. Records which provider actually handled the call in
    st.session_state['llm_providers_used'] (a list the UI can inspect after a batch of
    calls to show a small "fallback used" note) — reset that list before starting a
    user-facing operation if you want an accurate per-operation view."""
    errors = []
    for name, fn in (("openrouter", _call_openrouter), ("groq", _call_groq)):
        try:
            result = fn(prompt)
            try:
                st.session_state.setdefault("llm_providers_used", []).append(name)
            except Exception:
                pass
            return result
        except Exception as e:
            errors.append(f"{name}: {e}")

    raise RuntimeError(
        "Both AI providers failed or are not configured:\n" + "\n".join(errors)
    )


def parse_job(text: str, url: str) -> dict:
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

    result = call_llm(prompt)
    result["key_skills"] = format_bullets(result.get("key_skills", ""))
    result["comments"]   = format_bullets(result.get("comments", ""))
    return result


def _email_extract_prompt(email_text: str) -> str:
    return f"""You help track job applications. Extract structured information from this email
from a recruiter or company about a job application.

If the company name isn't spelled out in the email body, infer it from the sender's
email domain (e.g. "haakon@bbraun.com" → "B. Braun") and use that — recruiters
often only identify their company through the domain, not the visible text.

Email:
{email_text}

Return ONLY a JSON object with these fields:
{{
  "matched_company": "<company name from the email>",
  "matched_role": "<role/position from the email>",
  "contact_person": "<recruiter/HR name, phone, email from the signature — otherwise 'Not specified'>",
  "email_date": "<date the email was sent/received, YYYY-MM-DD format — read it from the email's own date/header/signature, not today's date>",
  "email_datetime": "<the email's own date AND time, YYYY-MM-DD HH:MM format (24h) — used only if this becomes a brand-new sheet row>",
  "new_status": "<updated status — one of: Applied, Interview, Assessment, Offer, Rejected, Withdrawn>",
  "status_confidence": "<high, medium, or low — confidence that new_status is the CORRECT reading of what this email says. High: explicit, unambiguous wording (e.g. \\"unfortunately we are unable to proceed with your application\\", \\"we would like to invite you to an interview\\"). Medium: status is strongly implied but not stated outright. Low: vague, templated, or boilerplate wording (e.g. \\"we will keep your profile on file\\", \\"your application is still being reviewed\\", auto-replies) where the real outcome is unclear — do not default to high just because a status must be picked>",
  "company_comments": "<concise summary of what THIS email says, one point per line starting with •\\n — do not include the email date, it is recorded separately, e.g.: • Interview invite: 2026-08-14 10:00 via Teams\\n• Next round: technical interview\\n• Rejection reason: overqualified>",
  "is_new_application_confirmation": <true/false — true only if this is an acknowledgment that a NEW application was just received (e.g. "Eingangsbestätigung", "we received your application", "thank you for applying"), not a status update on something already in progress>,
  "new_application_confidence": "<high, medium, or low — ONLY relevant when is_new_application_confirmation is true: how confident are you this is a genuinely new application, based on how clearly the company, role, and date are stated in the email>"
}}"""


def _looks_garbled(result: dict) -> bool:
    """Cheap sanity check for a technically-valid-JSON but semantically broken response —
    an occasional provider-level generation artifact observed in testing (e.g.
    matched_company coming back as "Morgan ? " instead of the full name, new_status
    missing entirely). Not exhaustive, just catches the obvious cases so one retry can
    recover instead of silently feeding garbage into matching / the sheet."""
    company = result.get("matched_company") or ""
    role = result.get("matched_role") or ""
    if "?" in company or "?" in role:
        return True
    return result.get("new_status") not in STATUSES


def parse_email(email_text: str, jobs: list) -> dict:
    # The AI only extracts from the email text itself — no job list is sent. It's
    # reliably accurate at that (company, role, status, dates, comments), which made
    # the previous design's job list pointless for every field except matched_row: a
    # row NUMBER cited out of a long (900+) candidate list, the one thing the model
    # was NOT reliably accurate at — it could confidently cite the wrong row while
    # still extracting the right company/role. So matched_row isn't asked of the AI at
    # all; it's resolved deterministically below via fuzzy_find_job's company+role text
    # match against the full job history, which is cheap, local, and doesn't share that
    # failure mode. This also means the prompt no longer scales with sheet size — no
    # per-provider budget juggling needed.
    prompt = _email_extract_prompt(email_text)
    result = call_llm(prompt)
    if _looks_garbled(result):
        result = call_llm(prompt)  # one retry — this failure mode has been observed to be transient
    result["company_comments"] = format_bullets(result.get("company_comments", ""))
    # Models don't always match the prompt's lowercase "high/medium/low" casing exactly;
    # normalize so the UI's dict lookups (keyed on lowercase) don't silently fall through.
    for field in ("status_confidence", "new_application_confidence"):
        if isinstance(result.get(field), str):
            result[field] = result[field].strip().lower()
    result["matched_row"] = fuzzy_find_job(result.get("matched_company", ""), result.get("matched_role", ""), jobs)
    return result


CORP_SUFFIX_RE = re.compile(r"\b(gmbh|se|ag|inc|ltd|llc|kg|corp|corporation|plc|co)\b")

def normalize_company(name: str) -> str:
    name = name.lower()
    name = CORP_SUFFIX_RE.sub("", name)
    return re.sub(r"[^a-z0-9]", "", name)


# Matches a German gender/diversity marker (m/w/d, w/m/d, m w d, ...) whether or not
# it's wrapped in parens — an email's own wording (e.g. a subject line) sometimes drops
# the parens/slashes the original job posting had, which would otherwise survive
# normalize_role's non-alnum stripping as stray letters ("mwd") breaking a substring match.
GENDER_MARKER_RE = re.compile(r"\b[mwdx](?:\s*/\s*|\s+)[mwdx](?:(?:\s*/\s*|\s+)[mwdx])?\b")


def normalize_role(role: str) -> str:
    role = role.lower()
    role = re.sub(r"\([^)]*\)", "", role)  # strip gender/diversity tags like "(w/m/d)"
    role = GENDER_MARKER_RE.sub("", role)  # ...and the same, unparenthesized, e.g. "m w d"
    return re.sub(r"[^a-z0-9]", "", role)


def _company_role_match(job: dict, target_co: str, target_role: str) -> bool:
    """target_co/target_role must already be normalize_company()/normalize_role()'d.
    True if job's company AND role both loosely match (substring either direction)."""
    co = normalize_company(str(job.get("Company", "")))
    role = normalize_role(str(job.get("Role", "")))
    return bool(co and role and (target_co in co or co in target_co) and (target_role in role or role in target_role))


def fuzzy_find_job(matched_company: str, matched_role: str, jobs: list):
    """Deterministic backstop for when the AI can't confidently match a row — e.g. the
    company is only identifiable via the sender's email domain (which this model doesn't
    infer reliably every time), or the row is outside the truncated candidate list sent
    to the AI (a genuine duplicate of an old application). Checks the FULL job history,
    not just the truncated list — cheap and local, no extra API call.

    Requires a minimum company-name length, since short names (e.g. "SAP") risk false
    substring matches (e.g. "Sapient"). If exactly one tracked application matches the
    company, that's returned directly without also requiring the role to match: an
    email's own wording of a role (e.g. from a subject line) can diverge a lot from the
    abbreviated title stored when the job was first added — different length, extra
    qualifiers, official vs. shortened title — and there's no ambiguity risk to guard
    against when there's only one row for that company in the first place. Only when a
    company has several separate tracked applications (a different role, an old
    rejected attempt) does the role also need to match, so as not to silently point at
    the wrong one."""
    target_co = normalize_company(matched_company or "")
    if len(target_co) < 4:
        return None
    co_matches = [j for j in jobs if (co := normalize_company(str(j.get("Company", "")))) and (target_co in co or co in target_co)]
    if len(co_matches) == 1:
        return co_matches[0].get("No.")

    target_role = normalize_role(matched_role or "")
    if len(target_role) < 4:
        return None
    candidates = [j.get("No.") for j in co_matches if _company_role_match(j, target_co, target_role)]
    unique_rows = set(candidates)
    return candidates[0] if len(unique_rows) == 1 else None


def match_job(parsed: dict, profile: str) -> dict:
    """Compare parsed job against the career profile. Returns match_level (0-100),
    match_summary, and missing_skills."""
    job_snapshot = (
        f"Role: {parsed.get('role', '')}\n"
        f"Company: {parsed.get('company', '')}\n"
        f"Language requirement: {parsed.get('language_req', '')}\n"
        f"Key skills required:\n{parsed.get('key_skills', '')}\n"
        f"Comments / requirements:\n{parsed.get('comments', '')}"
    )

    # No arbitrary truncation: the profile is a few thousand characters (well under
    # any provider's context window), and match accuracy matters more than the
    # negligible token-cost difference of sending it in full.
    prompt = f"""You are a career advisor performing a job-fit analysis.

CANDIDATE PROFILE:
{profile}

JOB POSTING:
{job_snapshot}

Analyse how well the candidate fits this job and return ONLY a JSON object:
{{
  "match_level": <integer 0–100, where 100 = perfect fit>,
  "match_summary": "<one concise sentence explaining the score>",
  "missing_skills": "<skills or requirements from the job the candidate clearly lacks — one per line starting with •. Write None if there are no gaps.>"
}}"""

    result = call_llm(prompt)
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


def _is_duplicate_submission(signature: tuple) -> bool:
    """True if this exact submission was already accepted in this session. append_job()
    has no dedup of its own — every call is a pure insert — so a double form submission
    (a slow save plus an impatient second click, or a network hiccup replaying the
    request before the UI had disabled the button) silently writes the same row twice.
    Guards both append_job() call sites in main() against exactly that."""
    return st.session_state.get("last_submission_sig") == signature


def _mark_submitted(signature: tuple) -> None:
    st.session_state["last_submission_sig"] = signature


def append_job(data: dict) -> int:
    """Inserts the job at the sheet position that keeps Date Applied ascending —
    appends at the bottom if the date is newest (the common case), otherwise inserts
    in the middle and renumbers every row pushed down so No. stays sequential."""
    ws = get_worksheet()
    ensure_extra_cols(ws)
    all_rows = ws.get_all_values()
    data_rows = [r for r in all_rows[1:] if any(cell.strip() for cell in r)]
    total_existing = len(data_rows)

    date_str = data.get("date_applied") or datetime.now(CET).strftime("%Y-%m-%d %H:%M")
    try:
        new_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
    except ValueError:
        date_str = datetime.now(CET).strftime("%Y-%m-%d %H:%M")
        new_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")

    insert_idx = total_existing
    for i, row in enumerate(data_rows):
        try:
            existing_dt = datetime.strptime(row[1], "%Y-%m-%d %H:%M")
        except (ValueError, IndexError):
            continue
        if new_dt < existing_dt:
            insert_idx = i
            break

    new_no = insert_idx + 1
    sheet_row = insert_idx + 2  # +1 for header, +1 for 1-indexing

    ml = data.get("match_level", "")
    match_display = f"{ml}%" if isinstance(ml, int) else str(ml)
    row_values = [
        new_no, date_str,
        data["company"], data["role"], data["city"],
        data["language_req"], data["key_skills"], data["contact_person"],
        data["url"], data["status"], data["comments"], data["cv_lang"],
        data.get("source", ""),          # M — Source
        "",                              # N — Company Comments
        match_display,                   # O — Match Level
        data.get("missing_skills", ""),  # P — Missing Skills
    ]

    if insert_idx == total_existing:
        ws.append_row(row_values, value_input_option="USER_ENTERED")
    else:
        ws.insert_row(row_values, sheet_row, value_input_option="USER_ENTERED", inherit_from_before=True)
        renumber_range = f"A{sheet_row + 1}:A{sheet_row + (total_existing - insert_idx)}"
        bumped = [[str(int(r[0]) + 1)] for r in ws.get(renumber_range) if r and r[0].strip().isdigit()]
        if bumped:
            ws.update(bumped, renumber_range, value_input_option="RAW")

    format_row(ws, sheet_row, [data["key_skills"], data["comments"], data.get("missing_skills", "")])
    return new_no


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

            st.session_state["llm_providers_used"] = []  # reset before this batch of AI calls

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

            st.session_state["parse_used_fallback"] = "groq" in st.session_state.get("llm_providers_used", [])

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
            if st.session_state.get("parse_used_fallback"):
                st.caption("⚙️ OpenRouter was unavailable for this request — used Groq fallback.")

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

            # Kept outside the form: a form's own widgets don't trigger a rerun until
            # it's submitted, so a checkbox inside the form can't reactively re-enable
            # the form's own submit button — checking it would never be "seen" before
            # the (still-disabled) submit click that would normally deliver it.
            proceed = st.checkbox("I know — add anyway") if dup else True

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

                date_applied = st.text_input(
                    "Date Applied (CET)",
                    value=datetime.now(CET).strftime("%Y-%m-%d %H:%M"),
                    help="Format: YYYY-MM-DD HH:MM. Backdating inserts the row in date "
                         "order and renumbers the rows below it — leave as-is for 'now'.",
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

                submitted = st.form_submit_button(
                    "✅ Add to Google Sheet", type="primary", use_container_width=True,
                    disabled=not proceed,
                )

            if submitted:
                try:
                    datetime.strptime(date_applied.strip(), "%Y-%m-%d %H:%M")
                except ValueError:
                    st.error("Date Applied must be in YYYY-MM-DD HH:MM format.")
                    st.stop()

                sig = ("add_job", company.strip().lower(), role.strip().lower(), job_url.strip(), date_applied.strip())
                if _is_duplicate_submission(sig):
                    st.session_state["success_msg"] = "Already added that one — skipped a duplicate submission."
                    st.session_state["input_key"] += 1
                    st.session_state.pop("parsed", None)
                    st.session_state.pop("duplicate", None)
                    st.rerun()

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
                            "date_applied": date_applied.strip(),
                        })
                        _mark_submitted(sig)
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

            st.session_state["llm_providers_used"] = []  # reset before this AI call

            with st.spinner("Parsing email with AI..."):
                try:
                    result = parse_email(email_text.strip(), jobs)
                    st.session_state["email_parsed"] = result
                    st.session_state["email_jobs"] = jobs
                    st.session_state["email_used_fallback"] = "groq" in st.session_state.get("llm_providers_used", [])
                except Exception as e:
                    st.error(f"Parsing failed: {e}")
                    st.stop()

        if "email_parsed" in st.session_state:
            r = st.session_state["email_parsed"]
            jobs = st.session_state.get("email_jobs", [])

            st.divider()
            st.subheader("Review & Apply Update")
            if st.session_state.get("email_used_fallback"):
                st.caption("⚙️ OpenRouter was unavailable for this request — used Groq fallback.")

            # matched_row was already resolved deterministically in parse_email() via
            # fuzzy_find_job (company AND role text match against the full job history)
            # — no AI row-number guessing involved.
            matched_row = r.get("matched_row")
            ai_is_new_app = r.get("is_new_application_confirmation", False)

            ADD_NEW_LABEL = "➕ Add as a new application (not in the sheet)"
            job_options = {
                f"Row {j.get('No.')} — {j.get('Company')} | {j.get('Role')} | {j.get('Status')}": j.get("No.")
                for j in jobs
            }
            options_list = [ADD_NEW_LABEL] + list(job_options.keys())

            if matched_row is None:
                default_idx = 0 if ai_is_new_app else 1
            else:
                default_idx = 1
                for i, no in enumerate(job_options.values()):
                    if no == matched_row:
                        default_idx = i + 1  # +1 for the "Add as new" option at index 0
                        break

            if matched_row is not None:
                st.info(
                    f"🔎 Matched by company **and** role — Row {matched_row}: "
                    f"**{r.get('matched_company', '')}** / **{r.get('matched_role', '')}**. Please "
                    f"confirm it's correct below, or pick \"{ADD_NEW_LABEL}\" if this is actually a "
                    f"different/new application."
                )
            elif ai_is_new_app:
                conf = r.get("new_application_confidence", "low")
                conf_color = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(conf, "🔴")
                st.info(
                    f"📥 This looks like a confirmation for a **new application not yet in your sheet**.\n\n"
                    f"Confidence this is genuinely untracked: {conf_color} **{conf.upper()}**"
                )
            else:
                st.warning(
                    f"⚠️ No matching application found for **{r.get('matched_company', 'this company')} — "
                    f"{r.get('matched_role', 'this role')}**. It may not be tracked yet, or the company/role "
                    f"wording doesn't closely match what's in your sheet — select the correct row below, or "
                    f"add it as a new application if it's missing entirely."
                )

            selected_label = st.selectbox(
                "Which job does this email refer to?",
                options_list,
                index=default_idx,
            )

            if selected_label == ADD_NEW_LABEL:
                with st.form("add_from_email_form"):
                    c1, c2 = st.columns(2)
                    with c1:
                        new_company = st.text_input("Company", value=r.get("matched_company", ""))
                        new_date = st.text_input(
                            "Date Applied (CET)", value=r.get("email_datetime", ""),
                            help="Format: YYYY-MM-DD HH:MM — from the email's own date/time.",
                        )
                    with c2:
                        new_role = st.text_input("Role", value=r.get("matched_role", ""))
                        new_contact = st.text_input("Contact Person", value=r.get("contact_person", "Not specified"))

                    new_comments = st.text_area(
                        "Comments", value=r.get("company_comments", ""), height=120,
                    )

                    add_btn = st.form_submit_button(
                        "➕ Add to Google Sheet", type="primary", use_container_width=True,
                    )

                if add_btn:
                    try:
                        datetime.strptime(new_date.strip(), "%Y-%m-%d %H:%M")
                    except ValueError:
                        st.error("Date Applied must be in YYYY-MM-DD HH:MM format.")
                        st.stop()

                    sig = ("add_from_email", new_company.strip().lower(), new_role.strip().lower(), new_date.strip())
                    if _is_duplicate_submission(sig):
                        st.session_state["success_msg"] = "Already added that one — skipped a duplicate submission."
                        st.session_state.pop("email_parsed", None)
                        st.session_state.pop("email_jobs", None)
                        st.session_state["email_key"] += 1
                        st.rerun()

                    with st.spinner("Adding to Google Sheet..."):
                        try:
                            row_no = append_job({
                                "company": new_company, "role": new_role, "city": "",
                                "language_req": "", "key_skills": "", "contact_person": new_contact,
                                "url": "", "status": "Applied", "comments": new_comments,
                                "cv_lang": st.session_state.get("cv_lang", "EN"), "source": "Other",
                                "match_level": "", "missing_skills": "",
                                "date_applied": new_date.strip(),
                            })
                            _mark_submitted(sig)
                            st.session_state["success_msg"] = f"🎉 Row #{row_no} added to Google Sheet!"
                            st.session_state.pop("email_parsed", None)
                            st.session_state.pop("email_jobs", None)
                            st.session_state["email_key"] += 1
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to add: {e}")

            else:
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
                        status_conf = r.get("status_confidence", "low")
                        status_conf_color = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(status_conf, "🔴")
                        st.caption(f"Status confidence: {status_conf_color} **{status_conf.upper()}**")

                    if status_conf == "low":
                        st.warning(
                            "⚠️ Low confidence that this status reading is correct — the email's "
                            "wording is vague or templated. Double-check it before applying."
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
