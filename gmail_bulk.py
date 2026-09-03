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

def scan_period(creds, label: str, start: date, end: date) -> dict:
    """Pure read: lists + fetches Gmail messages, extracts + matches each one via the
    reused app.py functions. NEVER calls update_job_from_email/append_job. One bad
    email is caught and recorded, never aborts the batch."""
    service = build_gmail_service(creds)
    query = build_query(label, start, end)
    processed_ids = load_processed_ids()
    own_email = get_own_email_address(service)

    ids = list_message_ids(service, query)
    results, failures = [], []
    skipped = 0
    sent_skipped = 0
    jobs_cache: dict = {}

    for msg_id in ids:
        if msg_id in processed_ids:
            skipped += 1
            continue

        subject_for_error = ""
        try:
            msg = fetch_message(service, msg_id)
            subject_for_error = msg.get("subject", "")
        except Exception as e:
            failures.append({"id": msg_id, "subject": subject_for_error, "error": str(e)})
            continue

        # Deterministic, no LLM cost: our own replies inside a labeled recruiter thread
        # (e.g. "thanks for the update") aren't job-status signals worth extracting.
        if own_email and _parse_email_address(msg["from"]) == own_email:
            sent_skipped += 1
            continue

        try:
            email_text = build_email_text(msg)
            info = app.extract_email_info(email_text)
        except Exception as e:
            failures.append({"id": msg_id, "subject": subject_for_error, "error": str(e)})
            continue

        target_sheet = app._archive_sheet_for_email_date(info.get("email_date", ""))
        cache_key = target_sheet or "__default__"
        if cache_key not in jobs_cache:
            try:
                jobs_cache[cache_key] = app.get_all_jobs(app.get_worksheet(target_sheet))
            except Exception as e:
                failures.append({"id": msg_id, "subject": subject_for_error, "error": f"Could not load sheet: {e}"})
                continue
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

    return {
        "results": results, "failures": failures, "skipped_processed": skipped,
        "sent_skipped": sent_skipped, "scanned": len(ids),
    }


# ── Grouping & proposals ─────────────────────────────────────────────────────

def _norm_key(company: str, role: str) -> tuple:
    return (app.normalize_company(company or ""), app.normalize_role(role or ""))


def _sorted_chrono(items: list) -> list:
    return sorted(items, key=lambda r: r["internal_date_ms"])


def _latest_existing_comment_date(company_comments: str) -> date | None:
    dates = COMMENT_DATE_RE.findall(company_comments or "")
    if not dates:
        return None
    try:
        return max(datetime.strptime(d, "%Y-%m-%d").date() for d in dates)
    except ValueError:
        return None


def _pick_best_contact(items: list) -> str:
    best = ""
    for r in items:
        c = (r["info"].get("contact_person") or "").strip()
        if app._is_blank_field(c):
            continue
        if not best:
            best = c
            continue
        score = lambda v: (("@" in v), (sum(ch.isdigit() for ch in v) >= 6), len(v))
        if score(c) > score(best):
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
            try:
                batch_dt = datetime.strptime(latest["info"].get("email_date", ""), "%Y-%m-%d").date()
            except ValueError:
                batch_dt = None
            if existing_latest and batch_dt and existing_latest > batch_dt:
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
    matched, unmatched_new, singles = {}, {}, []

    for r in results:
        if r["matched_row"] is not None:
            key = ("row", r["target_sheet"], r["matched_row"])
            matched.setdefault(key, []).append(r)
        elif not r["ambiguous_rows"] and r["info"].get("is_new_application_confirmation"):
            key = ("new", r["target_sheet"]) + _norm_key(
                r["info"].get("matched_company", ""), r["info"].get("matched_role", "")
            )
            unmatched_new.setdefault(key, []).append(r)
        else:
            singles.append(r)

    groups = [_build_group("matched", k, v) for k, v in matched.items()]
    groups += [_build_group("new", k, v) for k, v in unmatched_new.items()]
    groups += [_build_group("single", ("single", r["message_id"]), [r]) for r in singles]
    return groups


# ── Apply (the only writes in this module) ───────────────────────────────────

def _apply_matched(group: dict, row_no: int, target_sheet, overrides: dict) -> dict:
    applied = 0
    items = group["items"]
    comment_overrides = overrides.get("comments") or {}
    for idx, item in enumerate(items):
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
            ok, _filled = app.update_job_from_email(
                row_no, info.get("new_status", ""), comment_text,
                info.get("email_date", ""), sheet_name=target_sheet, email_info=info,
            )
        except Exception as e:
            return {"ok": False, "error": str(e), "applied": applied}
        if not ok:
            return {"ok": False, "error": f"Row #{row_no} not found in the sheet.", "applied": applied}
        log_processed_email(
            item["message_id"], item["thread_id"], info.get("email_date", ""),
            info.get("matched_company", ""), info.get("matched_role", ""),
            target_sheet or _default_sheet_title(), row_no, f"updated status={info.get('new_status', '')}",
        )
        applied += 1
    return {"ok": True, "error": None, "applied": applied}


def _apply_new(group: dict, target_sheet, overrides: dict) -> dict:
    items = group["items"]
    earliest = items[0]
    comment_overrides = overrides.get("comments") or {}
    combined_comments = "\n".join(
        line
        for i in items
        for line in (comment_overrides.get(i["message_id"], i["info"].get("company_comments", "")) or "").splitlines()
        if line.strip()
    )
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
        row_no = app.append_job(data, sheet_name=target_sheet)
    except Exception as e:
        return {"ok": False, "error": str(e), "applied": 0}
    for item in items:
        log_processed_email(
            item["message_id"], item["thread_id"], item["info"].get("email_date", ""),
            data["company"], data["role"], target_sheet or _default_sheet_title(), row_no, "created",
        )
    return {"ok": True, "error": None, "applied": len(items)}


def apply_group(group: dict, overrides: dict | None = None) -> dict:
    overrides = overrides or {}
    row_no = overrides.get("row_no") or group["matched_row"]
    if row_no and not overrides.get("force_new"):
        return _apply_matched(group, row_no, group["target_sheet"], overrides)
    if group["kind"] == "new" or overrides.get("force_new"):
        return _apply_new(group, group["target_sheet"], overrides)
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
        st.session_state["bulk_scan_period"] = period
        st.session_state["bulk_scan_custom_start"] = custom_start
        st.session_state["bulk_scan_custom_end"] = custom_end
        st.session_state["bulk_scan_label"] = label
        st.rerun()

    if not st.session_state.get("bulk_scanning"):
        return

    with st.spinner("Scanning Gmail — this can take a while for large date ranges..."):
        try:
            creds = get_valid_credentials()
            if not creds:
                app._flash("error", "Gmail connection expired — please reconnect.")
            else:
                start, end = _period_bounds(
                    st.session_state["bulk_scan_period"],
                    st.session_state.get("bulk_scan_custom_start"),
                    st.session_state.get("bulk_scan_custom_end"),
                )
                scan = scan_period(creds, st.session_state["bulk_scan_label"], start, end)
                groups = group_results(scan["results"])
                st.session_state["bulk_groups"] = groups
                st.session_state["bulk_scan_summary"] = {
                    "scanned": scan["scanned"],
                    "skipped": scan["skipped_processed"],
                    "sent_skipped": scan["sent_skipped"],
                    "failed": len(scan["failures"]),
                    "high": sum(1 for g in groups if g["bucket"] == "high"),
                    "review": sum(1 for g in groups if g["bucket"] == "review"),
                    "unmatched": sum(1 for g in groups if g["bucket"] == "unmatched"),
                }
                st.session_state["bulk_failures"] = scan["failures"]
                # A fresh scan always starts with a fresh decision state — setdefault()
                # here would let an Approve/Skip/Applied decision from a PREVIOUS scan
                # leak into this one whenever the same sheet row's group_key recurs
                # (e.g. the same application matched again), showing it as already
                # decided before this scan's results were even reviewed.
                st.session_state["bulk_group_decisions"] = {}
        except Exception as e:
            app._flash("error", f"Gmail scan failed: {e}")
        finally:
            st.session_state["bulk_scanning"] = False
            st.rerun()


def _render_scan_summary() -> None:
    s = st.session_state["bulk_scan_summary"]
    st.success(
        f"**{s['scanned']} emails scanned** — {s['skipped']} already processed (skipped), "
        f"{s.get('sent_skipped', 0)} sent messages skipped, {s['failed']} failed to parse.\n\n"
        f"🟢 {s['high']} high confidence · 🟡 {s['review']} need review · 🆕 {s['unmatched']} unmatched/new"
    )
    failures = st.session_state.get("bulk_failures", [])
    if failures:
        with st.expander(f"⚠️ {len(failures)} failed to parse — review manually"):
            for f in failures:
                st.caption(f"• {f.get('subject', '(no subject)')} — {f['error']}")


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

    sheet_label = g["target_sheet"] or _default_sheet_title()
    header = f"{g['company']} — {g['role']} · {sheet_label}"
    if g["matched_row"]:
        header += f" · Row {g['matched_row']}"
    header += f" · {g['email_count']} email(s)"
    icon = {"approve": "✅ ", "skip": "⏭️ ", "applied": "🟢 ", "failed": "❌ "}.get(decision["action"], "")

    with st.expander(f"{icon}{header}"):
        if decision["action"] in ("applied", "failed"):
            if decision["action"] == "applied":
                st.success("Applied.")
            else:
                st.error(f"Failed: {decision.get('error', 'unknown error')}")

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
                    key=f"bulk_existing_comments_{gk}", disabled=True, height=100,
                    label_visibility="collapsed",
                )
            else:
                st.caption("_(none yet)_")

        st.markdown("**Comments that will be appended if approved** — edit any of these before approving:")
        comment_edits = {}
        for item in g["items"]:
            info = item["info"]
            st.caption(f"📧 {info.get('email_date', '') or '?'} — {item['subject']}")
            comment_edits[item["message_id"]] = st.text_area(
                f"Comment for {item['message_id']}", value=info.get("company_comments", ""),
                key=f"bulk_comment_ov_{gk}_{item['message_id']}", height=80,
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
            choice = st.selectbox("Match to row", options, key=f"bulk_row_pick_{gk}")
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
                            key=f"bulk_amb_comments_{gk}", disabled=True, height=100,
                            label_visibility="collapsed",
                        )
                    else:
                        st.caption("_(no existing Company Comments)_")
        elif g["kind"] == "single" and g["matched_row"] is None:
            st.caption("No tracked application found for this company/role.")
            treat_as_new = st.checkbox(
                "Treat as a new application (create a row)", key=f"bulk_treat_new_{gk}", value=False,
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
            "Status (edit if needed)", status_options, index=default_status_idx, key=f"bulk_status_ov_{gk}",
        )
        edited_contact = st.text_input("Contact (edit if needed)", value=g["proposed_contact"], key=f"bulk_contact_ov_{gk}")

        can_approve = bool(selected_row) or treat_as_new
        b1, b2 = st.columns(2)
        with b1:
            if st.button("✅ Approve", key=f"bulk_approve_{gk}", disabled=not can_approve):
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
            if st.button("⏭️ Skip", key=f"bulk_skip_{gk}"):
                decisions[gk] = {"action": "skip"}
                st.rerun()

        if decision["action"] not in ("pending", "applied", "failed"):
            st.caption(f"Marked: **{decision['action']}**")


def _render_apply_controls() -> None:
    groups = st.session_state.get("bulk_groups", [])
    decisions = st.session_state.get("bulk_group_decisions", {})
    approved = [g for g in groups if decisions.get(g["group_key"], {}).get("action") == "approve"]

    st.divider()
    st.markdown(f"**{len(approved)} application(s)** marked for approval.")

    applying = st.session_state.get("bulk_applying", False)
    if st.button("🚀 Apply Approved Updates", type="primary", disabled=applying or not approved, key="bulk_apply_btn"):
        st.session_state["bulk_applying"] = True
        st.rerun()

    if not st.session_state.get("bulk_applying"):
        return

    with st.spinner(f"Applying {len(approved)} approved update(s)..."):
        results = [(g, apply_group(g, decisions[g["group_key"]])) for g in approved]

    ok_count = sum(1 for _, r in results if r["ok"])
    fail_count = len(results) - ok_count
    for g, r in results:
        decisions[g["group_key"]]["action"] = "applied" if r["ok"] else "failed"
        if not r["ok"]:
            decisions[g["group_key"]]["error"] = r["error"]

    st.session_state["bulk_groups"] = [
        g for g in groups if decisions.get(g["group_key"], {}).get("action") != "applied"
    ]
    app._flash(
        "success" if fail_count == 0 else "warning",
        f"Applied {ok_count} update(s)." + (f" {fail_count} failed — see below." if fail_count else ""),
    )
    st.session_state["bulk_applying"] = False
    st.rerun()
