"""Bulk historical Gmail -> Google Sheets update flow.

New, additive feature on top of the existing single-email "Update from Email" flow in
app.py. Reuses app.py's email extraction (extract_email_info), deterministic row
matching (fuzzy_find_job), archive-year routing (_archive_sheet_for_email_date), and the
same sheet-write functions (update_job_from_email, append_job) the single-email flow
already uses — this module never invents a second way to merge data into a row.

Gmail access is read-only (gmail.readonly scope only — never send/modify/delete/label).
Scanning Gmail NEVER writes to Google Sheets — only render_bulk_email_tab()'s "Apply
Approved Updates" button does, and only for groups the user explicitly approved.

`app` below is a placeholder, not a real import: under `streamlit run app.py` the
script executes as the module `__main__`, not as a module literally named `app` — a
plain `import app` here would make Python load app.py a second time from scratch as a
separate module (since `app` isn't yet in sys.modules under that name), re-running all
of its top-level setup code. app.py instead injects the live running module into
`gmail_bulk.app` right after importing this file (see the bottom of its import block),
so every app.<name> reference below resolves against the single real instance.
"""

import base64
import json
import re
import time
from datetime import date, datetime, timedelta

import streamlit as st
import gspread
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build as _gbuild

app = None  # injected by app.py at startup — see module docstring

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

PERIOD_2025 = "2025"
PERIOD_JAN_JUL_2026 = "Jan–Jul 2026"
PERIOD_CUSTOM = "Custom range"

# A hidden worksheet tab, not a local file: Streamlit Cloud's local disk is wiped on
# every redeploy (which happens on every git push to this repo), so a local token file
# would force reauthentication almost every time. The service account already has full
# read/write on this spreadsheet, so storing the refresh token here doesn't expand what
# was already trusted — it's just one more (hidden) tab. See README for the tradeoff.
TOKEN_SHEET_NAME = "_gmail_oauth_token"
TOKEN_SHEET_HEADER = ["token_json", "updated_at"]

IMPORT_LOG_NAME = "Email Import Log"
IMPORT_LOG_HEADER = [
    "Gmail Message ID", "Thread ID", "Email Date", "Company", "Role",
    "Target Worksheet", "Target Row", "Result/Action", "Processed At",
]

# Matches the exact "📧 YYYY-MM-DD |" prefix app.update_job_from_email() already writes
# into Company Comments — used to detect when the sheet already has a more recent
# update than a historical batch, so we never propose downgrading a later status.
COMMENT_DATE_RE = re.compile(r"📧\s*(\d{4}-\d{2}-\d{2})\s*\|")

QUOTE_MARKERS = re.compile(
    r"^\s*(On .{0,80} wrote:|-{2,}\s*Original Message\s*-{2,}|Von:.*Gesendet:|From:\s.*Sent:)",
    re.IGNORECASE | re.MULTILINE,
)

# Pulls the bare address out of a "From" header like "Jane Doe <jane@x.com>" — falls
# back to the raw header value if there's no <...> wrapper.
FROM_ADDR_RE = re.compile(r"<([^<>]+)>")


# ── Configuration ────────────────────────────────────────────────────────────

def _oauth_client_config() -> tuple:
    return (
        app._get_secret("GOOGLE_GMAIL_CLIENT_ID"),
        app._get_secret("GOOGLE_GMAIL_CLIENT_SECRET"),
        app._get_secret("GOOGLE_GMAIL_REDIRECT_URI"),
    )


def is_configured() -> bool:
    client_id, client_secret, redirect_uri = _oauth_client_config()
    return bool(client_id and client_secret and redirect_uri)


# ── OAuth ────────────────────────────────────────────────────────────────────

def build_flow() -> Flow:
    client_id, client_secret, redirect_uri = _oauth_client_config()
    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    # PKCE deliberately disabled: google-auth-oauthlib auto-generates a code_verifier
    # per Flow *instance* and only that same instance's fetch_token() has it in memory.
    # get_authorization_url() and handle_oauth_callback() each build their own Flow via
    # this function — often across a full page redirect to Google and back, sometimes
    # even across a re-login after Streamlit Cloud's session didn't survive that trip —
    # so a verifier generated on one instance is never available to the other, and
    # Google rejects the exchange with "(invalid_grant) Missing code verifier" (this
    # was a real bug here, not a hypothetical). PKCE exists to protect public clients
    # that can't hold a secret; this is a confidential "Web application" client that
    # already authenticates the token exchange with client_secret, so it doesn't need
    # PKCE on top — disabling it removes the requirement to carry a verifier across
    # requests/sessions at all.
    flow = Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, autogenerate_code_verifier=False)
    flow.redirect_uri = redirect_uri
    return flow


def get_authorization_url() -> str:
    flow = build_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    # Stored so the callback below can confirm the redirect it's handling actually
    # corresponds to an OAuth flow THIS browser session started, not a replayed or
    # forged callback URL.
    st.session_state["gmail_oauth_state"] = state
    return auth_url


def handle_oauth_callback() -> None:
    """Call once, early in main() — AFTER the Job Tracker password gate — before any
    tab renders. No-ops instantly unless the URL actually carries a fresh ?code=...
    from Google's redirect. Callers must ensure this only ever runs for an already-
    authenticated session: Gmail OAuth and Job Tracker login are separate concerns, and
    a bare callback URL must never grant tracker access by itself."""
    if not is_configured():
        return
    code = st.query_params.get("code")
    if not code:
        return
    incoming_state = st.query_params.get("state")
    expected_state = st.session_state.pop("gmail_oauth_state", None)
    try:
        if expected_state and incoming_state != expected_state:
            # We DO have a state from this session's own Connect-Gmail click, and it
            # doesn't match what came back — that's the genuine tamper/replay signal
            # worth blocking.
            st.session_state["flash"] = (
                "error",
                "Gmail connection failed: this authorization link couldn't be verified "
                "(state mismatch) — please click Connect Gmail again.",
            )
        else:
            # No stored state usually just means the browser's Streamlit session was
            # replaced during the round trip to Google — common on Streamlit Cloud,
            # where that external navigation can outlast the session, landing back on
            # the login screen. Re-entering the password creates a fresh session with
            # no memory of the state this callback's own query params still carry.
            # Reaching this line already required passing the Job Tracker password
            # gate, which is the real trust boundary here, so proceed with the
            # exchange rather than discard a legitimate callback.
            flow = build_flow()
            flow.fetch_token(code=code)
            creds = flow.credentials
            _save_stored_token_json(creds.to_json())
            st.session_state["gmail_creds_cache"] = creds
            st.session_state["flash"] = ("success", "✅ Gmail connected — read-only access granted.")
    except Exception as e:
        st.session_state["flash"] = ("error", f"Gmail connection failed: {e}")
    finally:
        st.query_params.pop("code", None)
        st.query_params.pop("state", None)
        st.query_params.pop("scope", None)
        st.query_params.pop("iss", None)
        st.rerun()


def get_valid_credentials() -> UserCredentials | None:
    if "gmail_creds_cache" in st.session_state:
        creds = st.session_state["gmail_creds_cache"]
    else:
        token_json = _load_stored_token_json()
        if not token_json:
            return None
        try:
            creds = UserCredentials.from_authorized_user_info(json.loads(token_json), GMAIL_SCOPES)
        except Exception:
            return None
        st.session_state["gmail_creds_cache"] = creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
            _save_stored_token_json(creds.to_json())
            st.session_state["gmail_creds_cache"] = creds
        except Exception:
            return None

    return creds if creds and creds.valid else None


def is_connected() -> bool:
    return get_valid_credentials() is not None


def disconnect() -> None:
    _clear_stored_token()
    st.session_state.pop("gmail_creds_cache", None)


# ── Worksheet helpers (token storage + import log) ──────────────────────────

def _get_or_create_worksheet(name: str, header: list, hidden: bool):
    base_ws = app.get_worksheet()  # any worksheet, just to get a handle on the spreadsheet
    sh = base_ws.spreadsheet
    try:
        ws = sh.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=200, cols=max(10, len(header)))
        ws.update([header], "A1")
        if hidden:
            sh.batch_update({
                "requests": [{
                    "updateSheetProperties": {
                        "properties": {"sheetId": ws.id, "hidden": True},
                        "fields": "hidden",
                    }
                }]
            })
    return ws


def _load_stored_token_json() -> str | None:
    ws = _get_or_create_worksheet(TOKEN_SHEET_NAME, TOKEN_SHEET_HEADER, hidden=True)
    values = ws.get_all_values()
    if len(values) < 2 or not values[1] or not values[1][0].strip():
        return None
    return values[1][0]


def _save_stored_token_json(token_json: str) -> None:
    ws = _get_or_create_worksheet(TOKEN_SHEET_NAME, TOKEN_SHEET_HEADER, hidden=True)
    ws.update([[token_json, datetime.now(app.CET).strftime("%Y-%m-%d %H:%M")]], "A2")


def _clear_stored_token() -> None:
    ws = _get_or_create_worksheet(TOKEN_SHEET_NAME, TOKEN_SHEET_HEADER, hidden=True)
    ws.update([["", ""]], "A2")


def _default_sheet_title() -> str:
    if "_gmail_default_sheet_title" not in st.session_state:
        try:
            st.session_state["_gmail_default_sheet_title"] = app.get_worksheet().title
        except Exception:
            return "current"
    return st.session_state["_gmail_default_sheet_title"]


def _import_log_ws():
    return _get_or_create_worksheet(IMPORT_LOG_NAME, IMPORT_LOG_HEADER, hidden=False)


def load_processed_ids() -> set:
    ws = _import_log_ws()
    values = ws.get_all_values()
    return {row[0].strip() for row in values[1:] if row and row[0].strip()}


def log_processed_email(msg_id, thread_id, email_date, company, role, target_sheet_label, target_row, action) -> None:
    ws = _import_log_ws()
    ws.append_row(
        [msg_id, thread_id, email_date or "", company or "", role or "",
         target_sheet_label, str(target_row) if target_row else "", action,
         datetime.now(app.CET).strftime("%Y-%m-%d %H:%M")],
        value_input_option="USER_ENTERED",
    )


# ── Gmail search & fetch ─────────────────────────────────────────────────────

def build_gmail_service(creds):
    return _gbuild("gmail", "v1", credentials=creds, cache_discovery=False)


def get_own_email_address(service) -> str:
    """The authenticated account's own address, via users().getProfile — used to skip
    our own sent messages (e.g. a reply inside a labeled recruiter thread) before they
    ever reach the LLM. Returns "" if the profile call fails for any reason; callers
    should treat that as "can't verify, don't filter" rather than fail the whole scan."""
    try:
        profile = service.users().getProfile(userId="me").execute()
        return (profile.get("emailAddress") or "").strip().lower()
    except Exception:
        return ""


def _parse_email_address(header_value: str) -> str:
    m = FROM_ADDR_RE.search(header_value or "")
    addr = m.group(1) if m else (header_value or "")
    return addr.strip().lower()


def _period_bounds(period: str, custom_start: date | None, custom_end: date | None) -> tuple:
    if period == PERIOD_2025:
        return date(2025, 1, 1), date(2026, 1, 1)
    if period == PERIOD_JAN_JUL_2026:
        return date(2026, 1, 1), date(2026, 8, 1)
    # Custom — Gmail's "before" is exclusive, so add a day to make the end date inclusive.
    return custom_start, custom_end + timedelta(days=1)


def build_query(label: str, start: date, end: date) -> str:
    label = (label or "").strip()
    label_part = f'label:"{label}" ' if label else ""
    return f'{label_part}after:{start.strftime("%Y/%m/%d")} before:{end.strftime("%Y/%m/%d")}'


def list_message_ids(service, query: str) -> list:
    ids = []
    page_token = None
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=100
        ).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _walk_parts(payload: dict):
    if payload.get("parts"):
        for part in payload["parts"]:
            yield from _walk_parts(part)
    else:
        yield payload.get("mimeType", ""), payload.get("body", {})


def extract_plain_text(payload: dict) -> str:
    plain, html = "", ""
    for mime, body in _walk_parts(payload):
        data = body.get("data")
        if not data:
            continue
        try:
            padded = data + "=" * (-len(data) % 4)
            text = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        except Exception:
            continue
        if mime == "text/plain" and not plain:
            plain = text
        elif mime == "text/html" and not html:
            html = text
    if plain.strip():
        return plain
    if html.strip():
        return app.html_to_text(html)
    return ""


def _header(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def fetch_message(service, msg_id: str) -> dict:
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId", ""),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "subject": _header(headers, "Subject"),
        "date_header": _header(headers, "Date"),
        "internal_date_ms": int(msg.get("internalDate", 0)),
        "body": extract_plain_text(payload),
    }


def strip_quoted_text(body: str) -> str:
    """Conservative truncation at common reply markers — avoids re-sending huge
    duplicated quoted history to the LLM without risking cutting a genuine message."""
    m = QUOTE_MARKERS.search(body)
    return body[:m.start()].rstrip() if m else body


def build_email_text(msg: dict) -> str:
    """Reassembles into the same "From/Date/Subject/To + body" shape the user already
    pastes manually into the single-email flow, so _email_extract_prompt needs no
    changes at all."""
    body = strip_quoted_text(msg["body"])
    header_block = (
        f"From: {msg['from']}\n"
        f"Date: {msg['date_header']}\n"
        f"Subject: {msg['subject']}\n"
        f"To: {msg['to']}\n"
    )
    return f"{header_block}\n{body}".strip()


# ── Scan (read-only) ─────────────────────────────────────────────────────────

def _scan_step(ctx: dict, msg_id: str) -> dict | None:
    """Processes exactly one Gmail message id, mutating ctx's "results"/"failures"/
    "counts"/"jobs_cache"/"ai_calls_attempted" in place. ctx must provide "service",
    "processed_ids" (set), "own_email" — everything scan_period() used to compute once
    up front. Returns a small "current" dict for progress display (date/company/role/
    subject only, never the body) or None when there's nothing worth showing (a skip).

    Factored out of scan_period() so the interactive scan (see _render_scan_controls,
    which drives this one message at a time, one Streamlit rerun per message) and the
    plain whole-range scan_period() below share the exact same per-message logic —
    the UI needs to process one message per rerun (not one big blocking call) so a
    Cancel click, which Streamlit can only act on between reruns, takes effect within
    roughly one message's latency instead of only after the entire scan finishes."""
    service = ctx["service"]
    processed_ids = ctx["processed_ids"]
    own_email = ctx["own_email"]
    results, failures, counts = ctx["results"], ctx["failures"], ctx["counts"]

    if msg_id in processed_ids:
        counts["already_processed_skipped"] += 1
        return None

    subject_for_error = ""
    try:
        msg = fetch_message(service, msg_id)
        subject_for_error = msg.get("subject", "")
    except Exception as e:
        failures.append({"id": msg_id, "subject": subject_for_error, "error": str(e), "stage": "fetch"})
        counts["failed"] += 1
        return None

    # Deterministic, no LLM cost: our own replies inside a labeled recruiter thread
    # (e.g. "thanks for the update") aren't job-status signals worth extracting.
    if own_email and _parse_email_address(msg["from"]) == own_email:
        counts["own_skipped"] += 1
        return {"date": msg.get("date_header", ""), "company": "", "role": "", "subject": msg.get("subject", "")}

    try:
        email_text = build_email_text(msg)
        ctx["ai_calls_attempted"] += 1
        info = app.extract_email_info(email_text)
    except Exception as e:
        failures.append({"id": msg_id, "subject": subject_for_error, "error": str(e), "stage": "extract"})
        counts["failed"] += 1
        return {"date": "", "company": "", "role": "", "subject": subject_for_error}

    target_sheet = app._archive_sheet_for_email_date(info.get("email_date", ""))
    cache_key = target_sheet or "__default__"
    jobs_cache = ctx["jobs_cache"]
    if cache_key not in jobs_cache:
        try:
            jobs_cache[cache_key] = app.get_all_jobs(app.get_worksheet(target_sheet))
        except Exception as e:
            failures.append({"id": msg_id, "subject": subject_for_error, "error": f"Could not load sheet: {e}", "stage": "sheet"})
            counts["failed"] += 1
            return None
    jobs = jobs_cache[cache_key]

    matched_row, ambiguous_rows = app.fuzzy_find_job(
        info.get("matched_company", ""), info.get("matched_role", ""), jobs,
        email_date=info.get("email_date", ""),
    )

    results.append({
        "message_id": msg["id"],
        "thread_id": msg["thread_id"],
        "subject": msg["subject"],
        "internal_date_ms": msg["internal_date_ms"],
        "target_sheet": target_sheet,
        "matched_row": matched_row,
        "ambiguous_rows": ambiguous_rows,
        "info": info,
    })
    counts["parsed_ok"] += 1
    return {
        "date": info.get("email_date", ""), "company": info.get("matched_company", ""),
        "role": info.get("matched_role", ""), "subject": msg.get("subject", ""),
    }


def scan_period(creds, label: str, start: date, end: date, progress_cb=None) -> dict:
    """Pure read, whole range in one call: lists + fetches Gmail messages, extracts +
    matches each one via the reused app.py functions (see _scan_step). NEVER calls
    update_job_from_email/append_job. One bad email is caught and recorded, never
    aborts the batch. Not used by the interactive UI (which processes one message per
    rerun instead, for cancellability — see _render_scan_controls) but kept as a plain
    whole-range entry point for tests and any non-interactive use.

    progress_cb, if given, is called synchronously (no threads/concurrency — sequential
    processing stays as-is, only visibility is added) at every point the loop advances,
    with a small dict: {"phase": "start", "total": N} once up front, then {"phase":
    "progress", "total", "processed", "parsed_ok", "own_skipped",
    "already_processed_skipped", "failed", "current"} after every message (success,
    skip, or failure alike). "current" is None or {"date", "company", "role",
    "subject"} only — never the email body, tokens, or raw exception text."""
    service = build_gmail_service(creds)
    query = build_query(label, start, end)
    ids = list_message_ids(service, query)
    if progress_cb:
        progress_cb({"phase": "start", "total": len(ids)})

    ctx = {
        "service": service, "processed_ids": load_processed_ids(), "own_email": get_own_email_address(service),
        "jobs_cache": {}, "results": [], "failures": [], "ai_calls_attempted": 0,
        "counts": {"parsed_ok": 0, "own_skipped": 0, "already_processed_skipped": 0, "failed": 0},
    }

    for processed, msg_id in enumerate(ids, start=1):
        current = _scan_step(ctx, msg_id)
        if progress_cb:
            progress_cb({"phase": "progress", "total": len(ids), "processed": processed, "current": current, **ctx["counts"]})

    return {
        "results": ctx["results"], "failures": ctx["failures"],
        "skipped_processed": ctx["counts"]["already_processed_skipped"],
        "sent_skipped": ctx["counts"]["own_skipped"], "scanned": len(ids),
        "ai_calls_attempted": ctx["ai_calls_attempted"],
    }


# ── Grouping & proposals ─────────────────────────────────────────────────────

def _norm_key(company: str, role: str) -> tuple:
    return (app.normalize_company(company or ""), app.normalize_role(role or ""))


def _best_event_dt(item: dict) -> datetime:
    """Best-available timestamp for one email, in the priority order the spec calls
    for: 1) the AI's own email_datetime (has real time-of-day), 2) the AI's own
    email_date (midnight), 3) Gmail's own internalDate as a deterministic fallback that
    never fails to parse. Used for chronological sort, timeline display, and the
    temporal-anchor grouping below — never for anything written to the sheet."""
    info = item["info"]
    dt_str = (info.get("email_datetime") or "").strip()
    if dt_str:
        try:
            return datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    d_str = (info.get("email_date") or "").strip()
    if d_str:
        try:
            return datetime.strptime(d_str, "%Y-%m-%d")
        except ValueError:
            pass
    return datetime.fromtimestamp(item["internal_date_ms"] / 1000)


def _sorted_chrono(items: list) -> list:
    return sorted(items, key=_best_event_dt)


def _latest_existing_comment_date(company_comments: str) -> date | None:
    dates = COMMENT_DATE_RE.findall(company_comments or "")
    if not dates:
        return None
    try:
        return max(datetime.strptime(d, "%Y-%m-%d").date() for d in dates)
    except ValueError:
        return None


def _pick_best_contact(items: list) -> str:
    """items must already be chronologically sorted (see _build_group). The most
    RECENT real contact wins — a later email naming a new recruiter supersedes an
    earlier one, even if the earlier text happened to look more "complete"."""
    best = ""
    for r in items:
        c = (r["info"].get("contact_person") or "").strip()
        if app._is_blank_field(c):
            continue
        best = c
    return best


def _build_group(kind: str, key: tuple, items: list) -> dict:
    items = _sorted_chrono(items)
    latest = items[-1]
    target_sheet = latest["target_sheet"]

    group = {
        "kind": kind,  # "matched" | "new" | "single"
        "group_key": "|".join(str(k) for k in key),
        "items": items,
        "target_sheet": target_sheet,
        "matched_row": latest["matched_row"] if kind == "matched" else None,
        "ambiguous_rows": latest["ambiguous_rows"],
        "company": latest["info"].get("matched_company", ""),
        "role": latest["info"].get("matched_role", ""),
        "proposed_status": latest["info"].get("new_status", ""),
        "status_confidence": latest["info"].get("status_confidence", "low"),
        "new_application_confidence": latest["info"].get("new_application_confidence", "low"),
        "proposed_contact": _pick_best_contact(items),
        "email_count": len(items),
        "emails_preview": [
            {"subject": r["subject"], "email_date": r["info"].get("email_date", ""), "id": r["message_id"]}
            for r in items
        ],
        "conflict": False,
        "current_status": "",
        "current_contact": "",
        "current_company_comments": "",
        "ambiguous_row_details": {},
    }

    if kind == "matched":
        try:
            jobs = app.get_all_jobs(app.get_worksheet(target_sheet))
            row = next((j for j in jobs if str(j.get("No.")) == str(latest["matched_row"])), None)
        except Exception:
            row = None
        if row:
            group["current_status"] = row.get("Status", "")
            group["current_contact"] = row.get("Contact Person", "")
            group["current_company_comments"] = row.get("Company Comments", "")
            existing_latest = _latest_existing_comment_date(group["current_company_comments"])
            # _best_event_dt always resolves to something (Gmail's own internalDate is
            # a deterministic last resort), so this batch date is available even when
            # the AI didn't extract a usable email_date — unlike checking
            # info["email_date"] alone, which used to silently skip the conflict check
            # whenever that one field was missing.
            batch_dt = _best_event_dt(latest).date()
            if existing_latest and existing_latest > batch_dt:
                group["conflict"] = True

    if group["ambiguous_rows"]:
        # Pull enough detail from the target worksheet to tell candidate rows apart —
        # "Row 183" and "Row 397" alone aren't enough to safely pick between two
        # similar applications.
        try:
            jobs = app.get_all_jobs(app.get_worksheet(target_sheet))
        except Exception:
            jobs = []
        for no in group["ambiguous_rows"]:
            row = next((j for j in jobs if str(j.get("No.")) == str(no)), None)
            if row:
                group["ambiguous_row_details"][no] = {
                    "company": row.get("Company", ""),
                    "role": row.get("Role", ""),
                    "status": row.get("Status", ""),
                    "date_applied": row.get("Date Applied", ""),
                    "contact": row.get("Contact Person", ""),
                    "company_comments": row.get("Company Comments", ""),
                }
        group["bucket"] = "review"
    elif kind == "matched":
        group["bucket"] = "high" if (group["status_confidence"] == "high" and not group["conflict"]) else "review"
    else:
        group["bucket"] = "unmatched"

    return group


def group_results(results: list) -> list:
    """Groups scanned emails into review items. Identity hierarchy (deterministic, no
    LLM decides this):

    1. A matched worksheet+row is the strongest signal — every email resolved to the
       same row is unquestionably one group, regardless of wording variation.
    2. Ambiguous matches (fuzzy_find_job found >1 candidate row) are NEVER folded into
       the temporal grouping below — they stay their own single review item so the user
       explicitly picks the row. Safety over convenience.
    3. For emails with no row match at all: a "new application confirmation" email
       (is_new_application_confirmation=true, not ambiguous) becomes an ANCHOR, keyed by
       (target_sheet, normalize_company, normalize_role) — exact string equality, not
       fuzzy_find_job's substring matching, so e.g. "Engineering Manager" vs "Senior
       Engineering Manager" never collapse into each other. The SAME key can have
       several anchors (one per confirmation email) — the same person can genuinely
       apply to the same company+role more than once, and each confirmation starts a
       new, separate application group rather than merging into an older one.
    4. Every other no-row-match email (not itself a confirmation) attaches to the most
       RECENT anchor of its own exact key that is dated on or before it — i.e. the
       still-open application as of that email's date. If no anchor qualifies (none
       exists yet for that key, or every anchor for that key is dated after it), the
       email stays its own single item for manual review rather than being guessed at.
       This is what turns a confirmation + a later "unfortunately..." for the same
       untracked company+role into one proposed new application, while keeping two
       separate applications to the same company+role (each with its own confirmation)
       as two distinct groups.
    """
    matched: dict = {}
    anchors: dict = {}   # norm key -> list of (anchor_dt, items) — one entry per anchor
    candidates: list = []
    review_singles: list = []

    for r in results:
        if r["matched_row"] is not None:
            key = ("row", r["target_sheet"], r["matched_row"])
            matched.setdefault(key, []).append(r)
        elif r["ambiguous_rows"]:
            review_singles.append(r)
        elif r["info"].get("is_new_application_confirmation"):
            key = (r["target_sheet"],) + _norm_key(
                r["info"].get("matched_company", ""), r["info"].get("matched_role", "")
            )
            anchors.setdefault(key, []).append(r)
        else:
            candidates.append(r)

    anchor_buckets: dict = {}  # key -> list of (anchor_dt, items_list), one per anchor
    for key, items in anchors.items():
        anchor_buckets[key] = [(_best_event_dt(it), [it]) for it in sorted(items, key=_best_event_dt)]

    for r in sorted(candidates, key=_best_event_dt):
        key = (r["target_sheet"],) + _norm_key(
            r["info"].get("matched_company", ""), r["info"].get("matched_role", "")
        )
        r_dt = _best_event_dt(r)
        best_anchor = None
        for anchor_dt, bucket in anchor_buckets.get(key, []):
            if anchor_dt <= r_dt and (best_anchor is None or anchor_dt > best_anchor[0]):
                best_anchor = (anchor_dt, bucket)
        if best_anchor:
            best_anchor[1].append(r)
        else:
            review_singles.append(r)

    groups = [_build_group("matched", k, v) for k, v in matched.items()]
    for key, bucket_list in anchor_buckets.items():
        for anchor_dt, items in bucket_list:
            # The anchor email's own message_id disambiguates multiple anchors that
            # share the same normalized company+role key (Part 10: repeated
            # applications), so each gets a distinct, stable group_key.
            group_key = ("new",) + key + (items[0]["message_id"],)
            groups.append(_build_group("new", group_key, items))
    groups += [_build_group("single", ("single", r["message_id"]), [r]) for r in review_singles]
    return groups


# ── Apply (the only writes in this module) ───────────────────────────────────

def _apply_with_retry(fn, *args, max_attempts=4, **kwargs):
    """Retries a Sheets-API write with exponential backoff (1s, 2s, 4s) when Google
    returns a rate-limit/transient error (429/500/503) — applying a large batch of
    consolidated groups after a big historical scan means many sequential writes
    (status, comments, backfills, formatting, import-log row — several gspread calls
    PER email), which can exceed Sheets' default ~60-writes/minute quota well before
    the batch finishes. Without this, one 429 mid-batch used to raise all the way up
    through apply_group() and crash the whole page (a real incident, not hypothetical —
    reported with a traceback landing exactly here)."""
    delay = 1.0
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status not in (429, 500, 503) or attempt == max_attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def _apply_matched(group: dict, row_no: int, target_sheet, overrides: dict, already_applied_ids: set | None = None) -> dict:
    applied = 0
    items = group["items"]
    comment_overrides = overrides.get("comments") or {}
    for idx, item in enumerate(items):
        if already_applied_ids and item["message_id"] in already_applied_ids:
            # This exact email was already successfully written and logged in a PRIOR
            # apply attempt (e.g. one that crashed partway through a large batch) —
            # re-running it would append a second, duplicate dated Company Comments
            # entry for the same email. Skip it; it's already reflected in the sheet.
            applied += 1
            continue
        info = dict(item["info"])
        if idx == len(items) - 1:
            if overrides.get("status"):
                info["new_status"] = overrides["status"]
            if overrides.get("contact"):
                info["contact_person"] = overrides["contact"]
        # The user may have edited this email's proposed comment text in the review
        # screen — that edited text, not the original AI extraction, is what's applied.
        comment_text = comment_overrides.get(item["message_id"], info.get("company_comments", ""))
        info["company_comments"] = comment_text
        try:
            ok, _filled = _apply_with_retry(
                app.update_job_from_email, row_no, info.get("new_status", ""), comment_text,
                info.get("email_date", ""), sheet_name=target_sheet, email_info=info,
            )
        except Exception as e:
            return {"ok": False, "error": str(e), "applied": applied}
        if not ok:
            return {"ok": False, "error": f"Row #{row_no} not found in the sheet.", "applied": applied}
        try:
            _apply_with_retry(
                log_processed_email, item["message_id"], item["thread_id"], info.get("email_date", ""),
                info.get("matched_company", ""), info.get("matched_role", ""),
                target_sheet or _default_sheet_title(), row_no, f"updated status={info.get('new_status', '')}",
            )
        except Exception:
            # The actual sheet update above already succeeded — that's what matters.
            # A missing import-log row just means a future scan might re-analyze this
            # one message (wasted LLM cost, not data loss) — far better than treating a
            # successful write as a failure, or letting this crash the whole batch.
            pass
        applied += 1
    return {"ok": True, "error": None, "applied": applied}


def _apply_new(group: dict, target_sheet, overrides: dict, already_applied_ids: set | None = None) -> dict:
    items = group["items"]
    if already_applied_ids and any(i["message_id"] in already_applied_ids for i in items):
        # A NEW-application group is a single append_job() call for the whole group —
        # unlike a matched group, there's no safe per-item granularity here. If any of
        # its emails is already logged, the row was already created in a prior attempt;
        # re-running would create a genuine DUPLICATE application row. Treat as done.
        return {"ok": True, "error": None, "applied": len(items), "already_applied": True}
    earliest = items[0]
    comment_overrides = overrides.get("comments") or {}
    seen_lines: set = set()
    combined_lines = []
    for i in items:
        text = comment_overrides.get(i["message_id"], i["info"].get("company_comments", "")) or ""
        for line in text.splitlines():
            if not line.strip():
                continue
            key = line.strip()
            # Multiple emails in the same consolidated timeline (e.g. an auto-ack
            # duplicating the confirmation's own wording) can carry an identical bullet
            # — skip an exact repeat rather than writing it into the sheet twice.
            if key in seen_lines:
                continue
            seen_lines.add(key)
            combined_lines.append(line)
    combined_comments = "\n".join(combined_lines)
    date_applied = earliest["info"].get("email_datetime", "") or ""
    if not date_applied:
        ed = earliest["info"].get("email_date", "")
        date_applied = f"{ed} 00:00" if ed else ""
    data = {
        "company": overrides.get("company") or group["company"],
        "role": overrides.get("role") or group["role"],
        "city": "", "language_req": "", "key_skills": "",
        "contact_person": overrides.get("contact") or group["proposed_contact"] or "Not specified",
        "url": "",
        "status": overrides.get("status") or group["proposed_status"] or "Applied",
        "comments": combined_comments,
        "cv_lang": "EN", "source": "Other", "match_level": "", "missing_skills": "",
        "date_applied": date_applied,
    }
    try:
        row_no = _apply_with_retry(app.append_job, data, sheet_name=target_sheet)
    except Exception as e:
        return {"ok": False, "error": str(e), "applied": 0}
    for item in items:
        try:
            _apply_with_retry(
                log_processed_email, item["message_id"], item["thread_id"], item["info"].get("email_date", ""),
                data["company"], data["role"], target_sheet or _default_sheet_title(), row_no, "created",
            )
        except Exception:
            # The row itself was already created above — see the matching comment in
            # _apply_matched for why a logging failure doesn't fail the whole group.
            pass
    return {"ok": True, "error": None, "applied": len(items)}


def apply_group(group: dict, overrides: dict | None = None, already_applied_ids: set | None = None) -> dict:
    overrides = overrides or {}
    row_no = overrides.get("row_no") or group["matched_row"]
    if row_no and not overrides.get("force_new"):
        return _apply_matched(group, row_no, group["target_sheet"], overrides, already_applied_ids)
    if group["kind"] == "new" or overrides.get("force_new"):
        return _apply_new(group, group["target_sheet"], overrides, already_applied_ids)
    return {"ok": False, "error": "No target row selected — pick a match or skip.", "applied": 0}


# ── UI ─────────────────────────────────────────────────────────────────────

def render_bulk_email_tab() -> None:
    if not is_configured():
        st.info(
            "📦 Gmail bulk update isn't configured yet. Add `GOOGLE_GMAIL_CLIENT_ID`, "
            "`GOOGLE_GMAIL_CLIENT_SECRET`, and `GOOGLE_GMAIL_REDIRECT_URI` to your "
            "secrets — see the README's \"Bulk Email Update (Gmail)\" section for setup."
        )
        return

    st.caption(
        "Scans your Gmail (read-only) for historical job-search emails and proposes "
        "sheet updates for you to review. Nothing is written to Google Sheets until "
        "you approve items below and click **Apply Approved Updates**."
    )

    connected = is_connected()
    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown(f"**Gmail:** {'✅ Connected' if connected else '❌ Not connected'}")
    with col2:
        if connected:
            if st.button("Disconnect", key="gmail_disconnect_btn"):
                disconnect()
                st.rerun()
        else:
            st.link_button("Connect Gmail", get_authorization_url())

    if not connected:
        return

    st.divider()
    _render_scan_controls()

    if "bulk_scan_summary" in st.session_state:
        _render_scan_summary()
        _render_review_queue()
        _render_apply_controls()


def _format_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _render_scan_controls() -> None:
    period = st.selectbox("Period", [PERIOD_2025, PERIOD_JAN_JUL_2026, PERIOD_CUSTOM], key="bulk_period")
    custom_start = custom_end = None
    if period == PERIOD_CUSTOM:
        c1, c2 = st.columns(2)
        with c1:
            custom_start = st.date_input("From", key="bulk_custom_start")
        with c2:
            custom_end = st.date_input("To (inclusive)", key="bulk_custom_end")
    label = st.text_input("Gmail label", value="Jobsearch", key="bulk_label")

    scanning = st.session_state.get("bulk_scanning", False)
    if st.button("🔍 Scan Gmail", disabled=scanning, key="bulk_scan_btn"):
        st.session_state["bulk_scanning"] = True
        st.session_state["bulk_scan_runtime"] = None  # (re)built on the first tick below
        st.session_state["bulk_scan_period"] = period
        st.session_state["bulk_scan_custom_start"] = custom_start
        st.session_state["bulk_scan_custom_end"] = custom_end
        st.session_state["bulk_scan_label"] = label
        st.rerun()

    if not st.session_state.get("bulk_scanning"):
        return

    runtime = st.session_state.get("bulk_scan_runtime")
    if runtime is None:
        # First tick of a new scan: everything up to (not including) per-message
        # processing — resolving creds, building the query, listing message ids. This
        # part is comparatively fast (a handful of paginated list calls, not one
        # fetch+LLM call per message) so it isn't itself chunked/cancellable; the long
        # part that follows is.
        creds = get_valid_credentials()
        if not creds:
            app._flash("error", "Gmail connection expired — please reconnect.")
            st.session_state["bulk_scanning"] = False
            st.rerun()
            return
        # Reset per-scan LLM counters (Part 2) so the summary below reflects only THIS
        # scan, not whatever accumulated in a previous one.
        st.session_state["llm_providers_used"] = []
        st.session_state["llm_extract_retries"] = 0
        start, end = _period_bounds(
            st.session_state["bulk_scan_period"],
            st.session_state.get("bulk_scan_custom_start"),
            st.session_state.get("bulk_scan_custom_end"),
        )
        label = st.session_state["bulk_scan_label"]
        query = build_query(label, start, end)
        try:
            service = build_gmail_service(creds)
            ids = list_message_ids(service, query)
            runtime = {
                "query": query, "ids": ids, "index": 0, "service": service,
                "processed_ids": load_processed_ids(), "own_email": get_own_email_address(service),
                "jobs_cache": {}, "results": [], "failures": [], "ai_calls_attempted": 0,
                "counts": {"parsed_ok": 0, "own_skipped": 0, "already_processed_skipped": 0, "failed": 0},
                "start_time": time.monotonic(), "current": None,
            }
        except Exception as e:
            app._flash("error", f"Gmail scan failed: {e}")
            st.session_state["bulk_scanning"] = False
            st.rerun()
            return
        st.session_state["bulk_scan_runtime"] = runtime

    total = len(runtime["ids"])
    processed = runtime["index"]
    st.caption(f"Gmail query: `{runtime['query']}`")

    cancel_col, _spacer = st.columns([1, 3])
    with cancel_col:
        cancel_clicked = st.button("🛑 Cancel scan", key="bulk_scan_cancel_btn")

    st.progress((processed / total) if total else 1.0)
    lines = [f"**Processing {processed} / {total} — {int(processed / total * 100) if total else 100}%**"]
    if total:
        c = runtime["counts"]
        lines.append(
            f"Parsed: {c['parsed_ok']} · Own sent skipped: {c['own_skipped']} · "
            f"Already processed: {c['already_processed_skipped']} · Failed: {c['failed']}"
        )
        current = runtime.get("current") or {}
        cur_bits = " — ".join(v for v in (current.get("date"), current.get("company"), current.get("role"), current.get("subject")) if v)
        if cur_bits:
            lines.append(f"Current: {cur_bits}")
        elapsed = time.monotonic() - runtime["start_time"]
        lines.append(f"Elapsed: {_format_hms(elapsed)}")
        if processed >= 10 and processed < total:
            remaining_min = max(1, round(elapsed / processed * (total - processed) / 60))
            lines.append(f"Estimated remaining: ~{remaining_min} min (approximate)")
    st.markdown("  \n".join(lines))

    def _finalize(cancelled: bool) -> None:
        groups = group_results(runtime["results"])
        elapsed = time.monotonic() - runtime["start_time"]
        providers = st.session_state.get("llm_providers_used", [])
        st.session_state["bulk_groups"] = groups
        st.session_state["bulk_scan_summary"] = {
            "scanned": total, "cancelled": cancelled, "processed_before_stop": processed,
            "skipped": runtime["counts"]["already_processed_skipped"],
            "sent_skipped": runtime["counts"]["own_skipped"],
            "failed": len(runtime["failures"]),
            "extraction_failures": sum(1 for f in runtime["failures"] if f.get("stage") == "extract"),
            "ai_calls_attempted": runtime["ai_calls_attempted"],
            "llm_retries": st.session_state.get("llm_extract_retries", 0),
            "openrouter_successes": providers.count("openrouter"),
            "groq_successes": providers.count("groq"),
            "groups": len(groups),
            "high": sum(1 for g in groups if g["bucket"] == "high"),
            "review": sum(1 for g in groups if g["bucket"] == "review"),
            "unmatched": sum(1 for g in groups if g["bucket"] == "unmatched"),
            "elapsed_seconds": elapsed,
        }
        st.session_state["bulk_failures"] = runtime["failures"]
        # A fresh scan always starts with a fresh decision state — setdefault() here
        # would let an Approve/Skip/Applied decision from a PREVIOUS scan leak into this
        # one whenever the same sheet row's group_key recurs (e.g. the same application
        # matched again), showing it as already decided before this scan's results were
        # even reviewed.
        st.session_state["bulk_group_decisions"] = {}
        # A group's group_key is deterministic (target_sheet + row, or company/role for
        # a new-application group), so the SAME key recurs whenever a later scan matches
        # the same application again. Streamlit widgets only honor index=/value= the
        # first time a given key is created — on a repeat key they silently keep
        # whatever the widget last held, which made the Status dropdown (and every other
        # per-group field) show a stale leftover choice from an earlier scan instead of
        # the freshly computed proposal. Bumping this counter gives every scan's widgets
        # a brand-new key namespace so their defaults are always honored.
        st.session_state["bulk_scan_id"] = st.session_state.get("bulk_scan_id", 0) + 1
        st.session_state["bulk_scan_runtime"] = None
        st.session_state["bulk_scanning"] = False

    if cancel_clicked:
        _finalize(cancelled=True)
        app._flash("warning", f"Scan cancelled after {processed}/{total} message(s) — partial results are shown below.")
        st.rerun()
        return

    if processed >= total:
        _finalize(cancelled=False)
        st.rerun()
        return

    # Process exactly ONE message this rerun, then trigger the next one. Each tick is
    # short (dominated by the message's own fetch+LLM latency, not by Streamlit's rerun
    # overhead), so the Cancel button above is always freshly rendered and interactive
    # again well before the next message finishes — unlike a single blocking call over
    # the whole range, where a widget click can't be acted on until it returns.
    runtime["current"] = _scan_step(runtime, runtime["ids"][processed])
    runtime["index"] += 1
    st.rerun()


def _render_scan_summary() -> None:
    s = st.session_state["bulk_scan_summary"]
    if s.get("cancelled"):
        st.warning(
            f"⚠️ Scan cancelled after {s.get('processed_before_stop', 0)} / {s['scanned']} message(s) — "
            "results below reflect only what was processed before you cancelled."
        )
    st.success(
        f"**{s['scanned']} Gmail messages found** — {s.get('ai_calls_attempted', 0)} analyzed by AI, "
        f"{s['skipped']} already processed, {s.get('sent_skipped', 0)} own sent skipped, "
        f"{s.get('extraction_failures', s['failed'])} extraction failures.\n\n"
        f"**{s.get('groups', s['high'] + s['review'] + s['unmatched'])} application group(s)** — "
        f"🟢 {s['high']} high confidence · 🟡 {s['review']} need review (incl. ambiguous) · "
        f"🆕 {s['unmatched']} unmatched/new.\n\n"
        f"LLM: {s.get('openrouter_successes', 0)} OpenRouter · {s.get('groq_successes', 0)} Groq fallback · "
        f"{s.get('llm_retries', 0)} retry/recovery — elapsed {_format_hms(s.get('elapsed_seconds', 0))}."
    )
    failures = st.session_state.get("bulk_failures", [])
    if failures:
        with st.expander(f"⚠️ {len(failures)} failed — review manually"):
            for f in failures:
                st.caption(f"• [{f.get('stage', '?')}] {f.get('subject', '(no subject)')} — {f['error']}")


def _render_review_queue() -> None:
    groups = st.session_state.get("bulk_groups", [])
    for bucket, title in (("review", "🟡 Needs Review"), ("high", "🟢 High Confidence"),
                           ("unmatched", "🆕 Unmatched / New Applications")):
        bucket_groups = [g for g in groups if g["bucket"] == bucket]
        if not bucket_groups:
            continue
        st.subheader(f"{title} ({len(bucket_groups)})")
        for g in bucket_groups:
            _render_group(g)


def _render_group(g: dict) -> None:
    decisions = st.session_state.setdefault("bulk_group_decisions", {})
    gk = g["group_key"]
    decision = decisions.get(gk, {"action": "pending"})
    # Namespaces every widget key below by the current scan generation — see the
    # bulk_scan_id comment in _render_scan_controls for why this is needed: a group's
    # group_key is deterministic and recurs across scans whenever the same application
    # matches again, and Streamlit only honors index=/value= the first time a given key
    # is created, so an un-namespaced key would silently keep a stale prior-scan value.
    sk = st.session_state.get("bulk_scan_id", 0)

    sheet_label = g["target_sheet"] or _default_sheet_title()
    email_word = "1 email" if g["email_count"] == 1 else f"{g['email_count']} emails consolidated"
    header = f"{g['company']} — {g['role']} · {sheet_label}"
    if g["matched_row"]:
        header += f" · Row {g['matched_row']}"
    header += f" · {email_word}"
    icon = {"approve": "✅ ", "skip": "⏭️ ", "applied": "🟢 ", "failed": "❌ "}.get(decision["action"], "")

    with st.expander(f"{icon}{header}"):
        if decision["action"] in ("applied", "failed"):
            if decision["action"] == "applied":
                st.success("Applied.")
            else:
                st.error(f"Failed: {decision.get('error', 'unknown error')}")

        if g["matched_row"]:
            st.caption(f"Matched: {sheet_label} sheet · row {g['matched_row']}")
        st.caption(email_word)

        if g["conflict"]:
            st.warning(
                "⚠️ The sheet already has a more recent update than this batch — "
                "review the proposed status carefully before approving."
            )

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**Current status:** {g['current_status'] or '_(none)_'}")
            st.markdown(f"**Current contact:** {g['current_contact'] or '_(none)_'}")
        with c2:
            st.markdown(f"**Proposed status:** {g['proposed_status'] or '_(unchanged)_'}")
            st.markdown(f"**Proposed contact:** {g['proposed_contact'] or '_(none — leaves existing)_'}")

        if g["kind"] == "matched":
            st.markdown("**Existing Company Comments** (already in the sheet — never erased):")
            if g["current_company_comments"].strip():
                st.text_area(
                    "Existing Company Comments", value=g["current_company_comments"],
                    key=f"bulk_existing_comments_{sk}_{gk}", disabled=True, height=100,
                    label_visibility="collapsed",
                )
            else:
                st.caption("_(none yet)_")

        # Read-only chronological overview — only worth showing once there's more than
        # one event to consolidate (a single-email group is already fully summarized by
        # "1 email" above, per the no-unnecessary-complexity rule).
        if len(g["items"]) > 1:
            st.markdown("**Timeline:**")
            for item in g["items"]:
                info = item["info"]
                event_date = info.get("email_date", "") or _best_event_dt(item).strftime("%Y-%m-%d")
                status = info.get("new_status", "") or "?"
                st.markdown(f"✓ {event_date} — {status}")
                for line in (info.get("company_comments", "") or "").splitlines():
                    if line.strip():
                        st.caption(f"　{line.strip()}")

        st.markdown("**Comments that will be appended if approved** — edit any of these before approving:")
        comment_edits = {}
        for item in g["items"]:
            info = item["info"]
            st.caption(f"📧 {info.get('email_date', '') or '?'} — {item['subject']}")
            comment_edits[item["message_id"]] = st.text_area(
                f"Comment for {item['message_id']}", value=info.get("company_comments", ""),
                key=f"bulk_comment_ov_{sk}_{gk}_{item['message_id']}", height=80,
                label_visibility="collapsed",
            )

        selected_row = g["matched_row"]
        treat_as_new = g["kind"] == "new"
        if g["ambiguous_rows"]:
            st.caption("Multiple tracked rows look like a match — pick the correct one, or leave unselected to skip:")
            details = g.get("ambiguous_row_details", {})

            def _row_label(no):
                d = details.get(no)
                if not d:
                    return f"Row {no}"
                return f"Row {no} — {d['company']} | {d['role']} | {d['status']} | {d['date_applied']}"

            options = ["— select —"] + [_row_label(n) for n in g["ambiguous_rows"]]
            choice = st.selectbox("Match to row", options, key=f"bulk_row_pick_{sk}_{gk}")
            if choice == "— select —":
                selected_row = None
            else:
                # Always the actual No. value from ambiguous_rows, never parsed back
                # out of the display label — the label is presentation only.
                selected_row = g["ambiguous_rows"][options.index(choice) - 1]
                picked = details.get(selected_row)
                if picked:
                    st.markdown(f"**Selected row's current Status:** {picked['status'] or '_(none)_'}")
                    st.markdown(f"**Selected row's current Contact Person:** {picked['contact'] or '_(none)_'}")
                    if picked["company_comments"].strip():
                        st.text_area(
                            "Selected row's Company Comments", value=picked["company_comments"],
                            key=f"bulk_amb_comments_{sk}_{gk}", disabled=True, height=100,
                            label_visibility="collapsed",
                        )
                    else:
                        st.caption("_(no existing Company Comments)_")
        elif g["kind"] == "single" and g["matched_row"] is None:
            st.caption("No tracked application found for this company/role.")
            treat_as_new = st.checkbox(
                "Treat as a new application (create a row)", key=f"bulk_treat_new_{sk}_{gk}", value=False,
            )

        status_options = list(app.STATUSES)
        proposed_status = g["proposed_status"]
        status_invalid = proposed_status not in status_options
        if not status_invalid:
            default_status_idx = status_options.index(proposed_status)
        elif g["current_status"] in status_options:
            # Safest fallback: don't guess a new status at all, propose no change.
            default_status_idx = status_options.index(g["current_status"])
        else:
            default_status_idx = status_options.index("Applied")
        if status_invalid:
            st.warning(
                f"⚠️ The AI returned an unrecognized status ('{proposed_status or 'empty'}') — "
                f"defaulted to **{status_options[default_status_idx]}** below. Please verify before approving."
            )
        edited_status = st.selectbox(
            "Status (edit if needed)", status_options, index=default_status_idx, key=f"bulk_status_ov_{sk}_{gk}",
        )
        edited_contact = st.text_input("Contact (edit if needed)", value=g["proposed_contact"], key=f"bulk_contact_ov_{sk}_{gk}")

        can_approve = bool(selected_row) or treat_as_new
        b1, b2 = st.columns(2)
        with b1:
            if st.button("✅ Approve", key=f"bulk_approve_{sk}_{gk}", disabled=not can_approve):
                decisions[gk] = {
                    "action": "approve",
                    "row_no": selected_row if not treat_as_new else None,
                    "force_new": treat_as_new,
                    "status": edited_status.strip(),
                    "contact": edited_contact.strip(),
                    "comments": comment_edits,
                }
                st.rerun()
        with b2:
            if st.button("⏭️ Skip", key=f"bulk_skip_{sk}_{gk}"):
                decisions[gk] = {"action": "skip"}
                st.rerun()

        if decision["action"] not in ("pending", "applied", "failed"):
            st.caption(f"Marked: **{decision['action']}**")


def _render_apply_controls() -> None:
    groups = st.session_state.get("bulk_groups", [])
    decisions = st.session_state.get("bulk_group_decisions", {})
    approved = [g for g in groups if decisions.get(g["group_key"], {}).get("action") == "approve"]
    failed = [g for g in groups if decisions.get(g["group_key"], {}).get("action") == "failed"]

    st.divider()
    st.markdown(f"**{len(approved)} application(s)** marked for approval.")

    if failed:
        st.warning(f"⚠️ {len(failed)} group(s) failed on the last Apply.")
        if st.button(f"🔁 Re-approve {len(failed)} failed group(s) for retry", key="bulk_reapprove_failed_btn"):
            # Each decision dict still holds its original row_no/status/contact/comments
            # from when it was first approved — only the action flips back, nothing the
            # user chose is lost. Safe to retry: already-applied emails within a
            # partially-failed group are skipped automatically (see already_applied_ids
            # in apply_group below), so this never re-writes what already succeeded.
            for g in failed:
                decisions[g["group_key"]]["action"] = "approve"
                decisions[g["group_key"]].pop("error", None)
            st.rerun()

    applying = st.session_state.get("bulk_applying", False)
    if st.button("🚀 Apply Approved Updates", type="primary", disabled=applying or not approved, key="bulk_apply_btn"):
        st.session_state["bulk_applying"] = True
        st.rerun()

    if not st.session_state.get("bulk_applying"):
        return

    # A previous attempt may have crashed partway through a large batch (a real
    # incident: a Sheets-API rate-limit error mid-apply used to raise all the way up
    # and take down the whole page). Whatever it already successfully logged is the
    # ground truth for "already applied" — apply_group() uses this to skip re-applying
    # those specific emails instead of writing duplicate comments/rows for them.
    try:
        already_applied_ids = load_processed_ids()
    except Exception:
        already_applied_ids = None  # can't check — proceed without the safety net rather than blocking Apply entirely

    status_ph = st.empty()
    results = []
    for idx, g in enumerate(approved, start=1):
        status_ph.markdown(f"Applying {idx} / {len(approved)}...")
        try:
            r = apply_group(g, decisions[g["group_key"]], already_applied_ids=already_applied_ids)
        except Exception as e:
            # Defense in depth: no single group's failure — expected (a row no longer
            # exists) or not — should ever crash the whole batch and lose every OTHER
            # group's already-recorded outcome below.
            r = {"ok": False, "error": str(e), "applied": 0}
        decisions[g["group_key"]]["action"] = "applied" if r["ok"] else "failed"
        if not r["ok"]:
            decisions[g["group_key"]]["error"] = r["error"]
        results.append((g, r))
    status_ph.empty()

    ok_count = sum(1 for _, r in results if r["ok"])
    fail_count = len(results) - ok_count

    st.session_state["bulk_groups"] = [
        g for g in groups if decisions.get(g["group_key"], {}).get("action") != "applied"
    ]
    app._flash(
        "success" if fail_count == 0 else "warning",
        f"Applied {ok_count} update(s)." + (f" {fail_count} failed — see below." if fail_count else ""),
    )
    st.session_state["bulk_applying"] = False
    st.rerun()
