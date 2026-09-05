#!/usr/bin/env python3
"""Lightweight, dependency-free checks for gmail_bulk.py's scan/grouping/apply logic.

Run with: python3 test_gmail_bulk.py

No live Gmail or Google Sheets access is used. app.py is loaded exactly the way the
running app injects it into gmail_bulk (see gmail_bulk.py's module docstring), so
normalize_company/normalize_role/fuzzy_find_job/_is_blank_field/_canonical_status are
the REAL logic — only the Sheets-network calls (get_worksheet/get_all_jobs) and the
Gmail-network calls (used in the scan_period tests) are replaced with small in-memory
stand-ins.
"""
import importlib.util
import inspect
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def _load_app_module():
    spec = importlib.util.spec_from_file_location("app", REPO_ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["app"] = module
    spec.loader.exec_module(module)
    return module


appmod = _load_app_module()

import gmail_bulk as gb  # noqa: E402

gb.app = appmod

JOBS_BY_SHEET: dict = {}
appmod.get_worksheet = lambda sheet_name=None: sheet_name
appmod.get_all_jobs = lambda ws: JOBS_BY_SHEET.get(ws, [])
# _default_sheet_title() reads st.session_state, which isn't available outside a real
# Streamlit script run — stub it so the apply-path tests below don't need one.
gb._default_sheet_title = lambda: "current"

failures = []


def check(name: str, condition: bool) -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def mk_info(company, role, status, email_date, confirmation=False, comments="", contact="Not specified", dt=""):
    return {
        "matched_company": company, "matched_role": role, "contact_person": contact,
        "email_date": email_date, "email_datetime": dt,
        "new_status": status, "status_confidence": "high",
        "company_comments": comments or f"• {status}",
        "is_new_application_confirmation": confirmation,
        "new_application_confidence": "high" if confirmation else "low",
    }


def mk_result(msg_id, subject, target_sheet, matched_row, ambiguous_rows, info, ms):
    return {
        "message_id": msg_id, "thread_id": msg_id + "-t", "subject": subject,
        "internal_date_ms": ms, "target_sheet": target_sheet,
        "matched_row": matched_row, "ambiguous_rows": ambiguous_rows, "info": info,
    }


def ms_for(y, m, d):
    return int(datetime(y, m, d).timestamp() * 1000)


# ── Part 11 fix: status-casing no longer trips the garbled-retry false positive ──
def test_canonical_status_fixes_case_sensitivity():
    check("lowercase status normalized to canonical casing", appmod._canonical_status("applied") == "Applied")
    check("already-canonical status left unchanged", appmod._canonical_status("Rejected") == "Rejected")
    check("unrecognized status left as-is (still triggers garbled detection)", appmod._canonical_status("N/A") == "N/A")
    check(
        "a differently-cased but valid status no longer looks garbled",
        not appmod._looks_garbled({"matched_company": "X", "matched_role": "Y", "new_status": appmod._canonical_status("applied")}),
    )


# ── Degenerate-repetition detection: a live production example where the LLM's
# comment text degenerated into a run of bare "…" tokens (status/comment both wrong)
# slipped past the old _looks_garbled check entirely — "Rejected" is a valid status and
# neither company nor role contained "?", so nothing caught it. ──
def test_degenerate_comment_text_triggers_garbled_retry():
    check(
        "a comment dominated by repeated ellipsis tokens is detected as degenerate",
        appmod._is_degenerate_text("Rejection : We … … … … … … … … … … … … … …"),
    )
    check(
        "a second real production example (mixed dot-runs) is also detected",
        appmod._is_degenerate_text("Acknowledgment receipt … …… … …………………………"),
    )
    check(
        "a normal short bullet comment is NOT flagged",
        not appmod._is_degenerate_text("• Interview invite: 2026-08-14 10:00 via Teams\n• Next round: technical interview"),
    )
    check(
        "a technically-valid status with degenerate comment text is still caught by _looks_garbled",
        appmod._looks_garbled({
            "matched_company": "CompuSafe Data Systems AG", "matched_role": "Not specified", "new_status": "Rejected",
            "company_comments": "Rejection : We … … … … … … … … … … … … … …",
        }),
    )


# ── Case 1: single email -> one group, Applied ──────────────────────────────
def test_single_email_one_group():
    JOBS_BY_SHEET[None] = []
    r = mk_result("m1", "App received", None, None, [], mk_info("Acme", "Data Analyst", "Applied", "2026-01-05", confirmation=True), ms_for(2026, 1, 5))
    groups = gb.group_results([r])
    check("single email -> exactly one group", len(groups) == 1)
    check("single email group proposed status Applied", groups[0]["proposed_status"] == "Applied")
    check("single email group has 1 item", groups[0]["email_count"] == 1)


# ── Case 2/3: matched row, 3-event timeline, final Rejected; role wording differs ──
def test_matched_row_timeline_and_role_wording():
    JOBS_BY_SHEET[None] = [{
        "No.": "10", "Company": "Acme GmbH", "Role": "Senior PM", "Status": "Applied",
        "Contact Person": "Not specified", "Company Comments": "", "Date Applied": "2026-01-01",
    }]
    items = [
        mk_result("m1", "Applied", None, 10, [], mk_info("Acme", "Senior Product Manager", "Applied", "2026-01-02"), ms_for(2026, 1, 2)),
        mk_result("m2", "Interview", None, 10, [], mk_info("Acme", "Senior PM (Product)", "Interview", "2026-01-10"), ms_for(2026, 1, 10)),
        mk_result("m3", "Rejected", None, 10, [], mk_info("Acme", "Sr. Product Manager", "Rejected", "2026-01-20"), ms_for(2026, 1, 20)),
    ]
    groups = gb.group_results(items)
    check("same matched row with differently-worded roles -> one group", len(groups) == 1)
    g = groups[0]
    check("timeline has 3 events", g["email_count"] == 3)
    check("final proposed status is the LATEST event, not a hierarchy", g["proposed_status"] == "Rejected")
    check("items are chronologically sorted", [it["message_id"] for it in g["items"]] == ["m1", "m2", "m3"])


# ── Case 4: same company, two different roles -> two groups ────────────────
def test_same_company_different_roles_stay_separate():
    JOBS_BY_SHEET[None] = []
    r1 = mk_result("m1", "EM app", None, None, [], mk_info("Beta Inc", "Engineering Manager", "Applied", "2026-02-01", confirmation=True), ms_for(2026, 2, 1))
    r2 = mk_result("m2", "Sr EM app", None, None, [], mk_info("Beta Inc", "Senior Engineering Manager", "Applied", "2026-02-02", confirmation=True), ms_for(2026, 2, 2))
    groups = gb.group_results([r1, r2])
    check("Engineering Manager vs Senior Engineering Manager stay separate", len(groups) == 2)


# ── Case 5: missing row, confirmation + later rejection, same company+role -> one new group ──
def test_confirmation_then_later_rejection_becomes_one_new_group():
    JOBS_BY_SHEET[None] = []
    confirm = mk_result("m1", "Thanks for applying", None, None, [], mk_info("Gamma AG", "Data Engineer", "Applied", "2026-03-01", confirmation=True), ms_for(2026, 3, 1))
    reject = mk_result("m2", "Unfortunately...", None, None, [], mk_info("Gamma AG", "Data Engineer", "Rejected", "2026-03-15"), ms_for(2026, 3, 15))
    groups = gb.group_results([confirm, reject])
    check("confirmation + later rejection -> one group", len(groups) == 1)
    g = groups[0]
    check("group kind is new", g["kind"] == "new")
    check("proposed status is Rejected", g["proposed_status"] == "Rejected")
    check("earliest item is the confirmation (Date Applied source)", g["items"][0]["message_id"] == "m1")


# ── Case 6: two separate applications, same company+role, months apart ─────
def test_two_separate_confirmations_same_company_role_stay_separate():
    JOBS_BY_SHEET[None] = []
    c1 = mk_result("m1", "Thanks", None, None, [], mk_info("Delta SE", "Consultant", "Applied", "2026-01-05", confirmation=True), ms_for(2026, 1, 5))
    r1 = mk_result("m2", "Rejected", None, None, [], mk_info("Delta SE", "Consultant", "Rejected", "2026-01-20"), ms_for(2026, 1, 20))
    c2 = mk_result("m3", "Thanks again", None, None, [], mk_info("Delta SE", "Consultant", "Applied", "2026-06-01", confirmation=True), ms_for(2026, 6, 1))
    groups = gb.group_results([c1, r1, c2])
    check("two confirmations months apart -> two groups", len(groups) == 2)
    by_first = {g["items"][0]["message_id"]: g for g in groups}
    check("the follow-up attaches to the FIRST confirmation, not the second", len(by_first.get("m1", {"items": []})["items"]) == 2)
    check("the second confirmation stands alone (no later email yet)", len(by_first.get("m3", {"items": []})["items"]) == 1)


# ── Real incidents: two confirmation-shaped emails close together in time for the same
# application (an auto-ack plus a separate "thank you" notice) must consolidate into
# ONE group instead of each creating its own duplicate row ──
def test_close_together_confirmations_cluster_into_one_group():
    JOBS_BY_SHEET[None] = []
    c1 = mk_result("m1", "Thanks", None, None, [], mk_info("N26", "Agile Coach", "Applied", "2026-01-20", confirmation=True), ms_for(2026, 1, 20))
    c2 = mk_result("m2", "Thanks again", None, None, [], mk_info("N26", "Agile Coach", "Applied", "2026-01-21", confirmation=True), ms_for(2026, 1, 21))
    groups = gb.group_results([c1, c2])
    check("two confirmations 1 day apart cluster into one group", len(groups) == 1)
    check("the clustered group has both emails", groups[0]["email_count"] == 2)


def test_confirmations_beyond_cluster_window_stay_separate():
    JOBS_BY_SHEET[None] = []
    c1 = mk_result("m1", "Thanks", None, None, [], mk_info("Cubiq Recruitment", "Engineering Manager", "Applied", "2026-01-02", confirmation=True), ms_for(2026, 1, 2))
    c2 = mk_result("m2", "Thanks", None, None, [], mk_info("Cubiq Recruitment", "Engineering Manager", "Applied", "2026-01-08", confirmation=True), ms_for(2026, 1, 8))
    groups = gb.group_results([c1, c2])
    check("confirmations 6 days apart (beyond the cluster window) stay two separate groups", len(groups) == 2)


def test_apply_new_falls_back_to_internal_date_not_now():
    # Both AI date fields empty — must not silently fall through to append_job()'s own
    # "now" default (a real incident: a row ended up dated the moment Apply was
    # clicked instead of the actual, months-earlier email date).
    JOBS_BY_SHEET[None] = []
    appended = []
    appmod.append_job = lambda data, sheet_name=None: (appended.append(data), 1)[1]
    gb.log_processed_email = lambda *a, **k: None
    info = mk_info("NoDateCo", "Some Role", "Applied", "", confirmation=True)
    info["email_datetime"] = ""
    ms = ms_for(2026, 1, 31)
    item = mk_result("m1", "s", None, None, [], info, ms)
    group = gb._build_group("new", ("new", None, "nodateco", "somerole", "m1"), [item])
    gb.apply_group(group, {"comments": {}})
    expected = datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    check("date_applied falls back to the Gmail internalDate, not 'now'", appended[0]["date_applied"] == expected)


# ── Cross-year consolidation ("Playson"): a 2025 confirmation + a 2026 status email
# for the same normalized company+role must stay ONE group, homed in the 2025 sheet ──
def test_cross_year_confirmation_and_rejection_consolidate_into_2025():
    JOBS_BY_SHEET[None] = []
    confirm = mk_result("m1", "Thanks for applying", "2025", None, [], mk_info("Playson", "Game Developer", "Applied", "2025-12-18", confirmation=True), ms_for(2025, 12, 18))
    reject = mk_result("m2", "Unfortunately...", None, None, [], mk_info("Playson", "Game Developer", "Rejected", "2026-01-15"), ms_for(2026, 1, 15))
    groups = gb.group_results([confirm, reject])
    check("2025 confirmation + 2026 rejection consolidate into one group", len(groups) == 1)
    g = groups[0] if groups else {}
    check("the group's home sheet is 2025 (the application's own year), not 2026", g.get("target_sheet") == "2025")
    check("final proposed status is the latest event (Rejected)", g.get("proposed_status") == "Rejected")
    check("the timeline has both emails", g.get("email_count") == 2)


def test_cross_year_fallback_bounded_to_jan_feb():
    # A real matching row exists in the 2025 sheet, but the rejection is dated March —
    # outside the Jan/Feb window — so the fallback must NOT fire; it stays an orphan.
    JOBS_BY_SHEET[None] = []
    JOBS_BY_SHEET["2025"] = [{
        "No.": "77", "Company": "Zorro Inc", "Role": "Analyst", "Status": "Applied",
        "Contact Person": "", "Company Comments": "", "Date Applied": "2025-11-01",
    }]
    reject = mk_result("m1", "Unfortunately...", None, None, [], mk_info("Zorro Inc", "Analyst", "Rejected", "2026-03-10"), ms_for(2026, 3, 10))
    groups = gb.group_results([reject])
    check("a March-2026 orphan does NOT fall back to the 2025 sheet (bounded to Jan/Feb)", len(groups) == 1 and groups[0]["kind"] == "single" and groups[0]["matched_row"] is None)


def test_jan_feb_2026_fallback_matches_2025_row():
    JOBS_BY_SHEET[None] = []
    JOBS_BY_SHEET["2025"] = [{
        "No.": "88", "Company": "Nordwind AG", "Role": "Backend Engineer", "Status": "Applied",
        "Contact Person": "", "Company Comments": "", "Date Applied": "2025-11-15",
    }]
    reject = mk_result("m1", "Unfortunately...", None, None, [], mk_info("Nordwind AG", "Backend Engineer", "Rejected", "2026-02-05"), ms_for(2026, 2, 5))
    groups = gb.group_results([reject])
    check("a Feb-2026 rejection with no 2026 match falls back and matches the 2025 row", len(groups) == 1 and groups[0]["kind"] == "matched")
    check("the fallback-matched group's target_sheet is 2025", groups[0]["target_sheet"] == "2025")
    check("the fallback-matched group's matched_row is row 88", str(groups[0]["matched_row"]) == "88")


# ── Low-value review-queue filtering ────────────────────────────────────────
def test_low_value_rejected_to_applied_skipped():
    JOBS_BY_SHEET[None] = [{
        "No.": "60", "Company": "Kappa3", "Role": "Ops", "Status": "Rejected",
        "Contact Person": "", "Company Comments": "", "Date Applied": "2025-05-01",
    }]
    r = mk_result("m1", "s", None, 60, [], mk_info("Kappa3", "Ops", "Applied", "2026-01-01"), ms_for(2026, 1, 1))
    keep, skipped = gb._partition_low_value(gb.group_results([r]))
    check("Rejected -> Applied is filtered as low-value noise", len(keep) == 0 and len(skipped) == 1)


def test_low_value_applied_to_applied_filtered_unless_contact_backfill():
    JOBS_BY_SHEET[None] = [{
        "No.": "61", "Company": "Lambda2", "Role": "Support", "Status": "Applied",
        "Contact Person": "Not specified", "Company Comments": "", "Date Applied": "2025-05-01",
    }]
    no_contact = mk_result("m1", "s", None, 61, [], mk_info("Lambda2", "Support", "Applied", "2026-01-01", contact="Not specified"), ms_for(2026, 1, 1))
    keep1, skipped1 = gb._partition_low_value(gb.group_results([no_contact]))
    check("Applied -> Applied with nothing new is filtered as low-value", len(keep1) == 0 and len(skipped1) == 1)

    with_contact = mk_result("m2", "s", None, 61, [], mk_info("Lambda2", "Support", "Applied", "2026-01-02", contact="Jane Doe <jane@x.com>"), ms_for(2026, 1, 2))
    keep2, skipped2 = gb._partition_low_value(gb.group_results([with_contact]))
    check("Applied -> Applied that backfills a blank contact is kept", len(keep2) == 1 and len(skipped2) == 0)


def test_low_value_new_group_with_blank_role_skipped():
    JOBS_BY_SHEET[None] = []
    blank_role = mk_result("m1", "s", None, None, [], mk_info("Mu Corp", "", "Applied", "2026-01-01", confirmation=True), ms_for(2026, 1, 1))
    keep1, skipped1 = gb._partition_low_value(gb.group_results([blank_role]))
    check("a new application with no identifiable role is filtered as low-value", len(keep1) == 0 and len(skipped1) == 1)

    real_role = mk_result("m2", "s", None, None, [], mk_info("Mu Corp", "Data Analyst", "Applied", "2026-01-01", confirmation=True), ms_for(2026, 1, 1))
    keep2, skipped2 = gb._partition_low_value(gb.group_results([real_role]))
    check("a new application with a real role is kept", len(keep2) == 1 and len(skipped2) == 0)


def test_low_value_role_copied_from_subject_treated_as_blank():
    # Live production example: comdesk's email named no actual position anywhere, so
    # the AI copied the generic subject line itself into matched_role — this backstop
    # catches that even if the prompt fix doesn't (belt and suspenders).
    JOBS_BY_SHEET[None] = []
    subject = "Deine Bewerbung bei comdesk"
    copied = mk_result("m1", subject, None, None, [], mk_info("comdesk", subject, "Applied", "2025-12-31", confirmation=True), ms_for(2025, 12, 31))
    keep, skipped = gb._partition_low_value(gb.group_results([copied]))
    check("a role that's just the email's own subject copied verbatim is treated as blank", len(keep) == 0 and len(skipped) == 1)


def test_low_value_reason_is_human_readable_for_each_rule():
    JOBS_BY_SHEET[None] = [{
        "No.": "70", "Company": "Reason Co", "Role": "Ops", "Status": "Rejected",
        "Contact Person": "", "Company Comments": "", "Date Applied": "2025-05-01",
    }]
    r = mk_result("m1", "s", None, 70, [], mk_info("Reason Co", "Ops", "Applied", "2026-01-01"), ms_for(2026, 1, 1))
    g = gb.group_results([r])[0]
    check("reason names the Rejected->Applied regression", "Rejected" in gb._low_value_reason(g) and "Applied" in gb._low_value_reason(g))

    blank_role = mk_result("m2", "s", None, None, [], mk_info("Reason Co 2", "", "Applied", "2026-01-01", confirmation=True), ms_for(2026, 1, 1))
    g2 = gb.group_results([blank_role])[0]
    check("reason names the missing role", "role" in gb._low_value_reason(g2).lower())


# ── Case 7: ambiguous match -> stays its own review item, never silently merged ──
def test_ambiguous_never_merged():
    JOBS_BY_SHEET[None] = []
    r = mk_result("m1", "Update", None, None, [11, 12], mk_info("Epsilon", "Analyst", "Interview", "2026-04-01"), ms_for(2026, 4, 1))
    groups = gb.group_results([r])
    check("ambiguous match stays its own group", len(groups) == 1)
    check("ambiguous group bucket is review", groups[0]["bucket"] == "review")
    check("ambiguous_rows preserved for the user to pick", groups[0]["ambiguous_rows"] == [11, 12])


# ── Case 8: existing sheet comment newer than this batch -> conflict flag stays ──
def test_conflict_protection_kept():
    JOBS_BY_SHEET[None] = [{
        "No.": "20", "Company": "Zeta", "Role": "Coach", "Status": "Offer",
        "Contact Person": "Jane", "Company Comments": "📧 2026-05-20 | Status: Offer\nGot an offer",
        "Date Applied": "2026-04-01",
    }]
    r = mk_result("m1", "Interview invite", None, 20, [], mk_info("Zeta", "Coach", "Interview", "2026-05-01"), ms_for(2026, 5, 1))
    groups = gb.group_results([r])
    check("stale batch vs newer sheet record -> conflict flagged", groups[0]["conflict"] is True)


# ── Case 9/10: own-sent / already-processed messages skipped before any LLM call ──
def test_skip_before_llm():
    JOBS_BY_SHEET[None] = []

    def fake_extract(text):
        raise AssertionError("extract_email_info must not be called for a skipped message")

    def fake_fetch(service, msg_id):
        if msg_id == "MSG_PROCESSED":
            raise AssertionError("fetch_message must not be called for an already-processed id")
        return {
            "id": msg_id, "thread_id": "t1", "from": "Me <me@example.com>", "to": "",
            "subject": "Re: thanks", "date_header": "Mon, 1 Jan 2026 10:00:00 +0000",
            "internal_date_ms": ms_for(2026, 1, 1), "body": "no worries",
        }

    appmod.extract_email_info = fake_extract
    gb.build_gmail_service = lambda creds: object()
    gb.get_own_email_address = lambda service: "me@example.com"
    gb.load_processed_ids = lambda: {"MSG_PROCESSED"}
    gb.list_message_ids = lambda service, query: ["MSG_PROCESSED", "MSG_OWN_SENT"]
    gb.fetch_message = fake_fetch

    result = gb.scan_period(object(), "Jobsearch", date(2026, 1, 1), date(2026, 2, 1))
    check("already-processed message skipped, never fetched", result["skipped_processed"] == 1)
    check("own-sent message skipped, LLM never called", result["sent_skipped"] == 1)
    check("no results produced from skipped messages", result["results"] == [])


# ── Case 11: extraction failure doesn't abort the scan ─────────────────────
def test_extraction_failure_continues_scan():
    JOBS_BY_SHEET[None] = []

    def fake_fetch(service, msg_id):
        return {
            "id": msg_id, "thread_id": "t1", "from": "Recruiter <r@company.com>", "to": "",
            "subject": f"Subj {msg_id}", "date_header": "Mon, 1 Jan 2026 10:00:00 +0000",
            "internal_date_ms": ms_for(2026, 1, 1), "body": "body",
        }

    def fake_extract(text):
        if "FAIL" in text:
            raise RuntimeError("both providers failed: simulated")
        return mk_info("Eta", "Analyst", "Applied", "2026-01-01", confirmation=True)

    appmod.extract_email_info = fake_extract
    gb.build_gmail_service = lambda creds: object()
    gb.get_own_email_address = lambda service: ""
    gb.load_processed_ids = lambda: set()
    gb.list_message_ids = lambda service, query: ["OK1", "FAIL1", "OK2"]
    gb.fetch_message = fake_fetch

    progress_events = []
    result = gb.scan_period(
        object(), "Jobsearch", date(2026, 1, 1), date(2026, 2, 1),
        progress_cb=lambda u: progress_events.append(u),
    )
    check("scan continues past one failing email", len(result["results"]) == 2)
    check("failure recorded with extract stage", result["failures"][0]["stage"] == "extract")
    check("progress callback fires for the start event plus every message", len(progress_events) == 4)


# ── Cancel support: chunked one-message-per-tick processing (what the UI now does,
# one Streamlit rerun per message, so a Cancel click lands within ~one message's
# latency) must produce identical results to the whole-range scan_period() call ──
def test_chunked_scan_step_matches_whole_range_scan():
    JOBS_BY_SHEET[None] = []

    def fake_fetch(service, msg_id):
        return {
            "id": msg_id, "thread_id": "t1", "from": "Recruiter <r@company.com>", "to": "",
            "subject": f"Subj {msg_id}", "date_header": "Mon, 1 Jan 2026 10:00:00 +0000",
            "internal_date_ms": ms_for(2026, 1, 1), "body": "body",
        }

    appmod.extract_email_info = lambda text: mk_info("Kappa2", "Eng", "Applied", "2026-01-01", confirmation=True)
    gb.build_gmail_service = lambda creds: object()
    gb.get_own_email_address = lambda service: ""
    gb.load_processed_ids = lambda: set()
    ids = ["c1", "c2", "c3"]
    gb.list_message_ids = lambda service, query: list(ids)
    gb.fetch_message = fake_fetch

    whole = gb.scan_period(object(), "Jobsearch", date(2026, 1, 1), date(2026, 2, 1))

    # Replay the same ids one _scan_step call at a time — exactly what the chunked UI
    # loop does across separate reruns via st.session_state["bulk_scan_runtime"].
    ctx = {
        "service": object(), "processed_ids": set(), "own_email": "",
        "jobs_cache": {}, "results": [], "failures": [], "ai_calls_attempted": 0,
        "counts": {"parsed_ok": 0, "own_skipped": 0, "already_processed_skipped": 0, "failed": 0},
    }
    for msg_id in ids:
        gb._scan_step(ctx, msg_id)

    check("chunked scan produces the same result count as scan_period", len(ctx["results"]) == len(whole["results"]) == 3)
    check(
        "chunked scan produces the same message ids, in order, as scan_period",
        [r["message_id"] for r in ctx["results"]] == [r["message_id"] for r in whole["results"]],
    )
    check("chunked scan's parsed_ok count matches scan_period's ai_calls_attempted", ctx["counts"]["parsed_ok"] == whole["ai_calls_attempted"] == 3)


def test_cancel_partial_results_are_still_valid_groups():
    # A cancelled scan finalizes with whatever's in runtime["results"] so far — confirm
    # group_results() handles a partial (stopped mid-way) list exactly like a complete
    # one, i.e. cancelling doesn't corrupt or special-case the grouping logic at all.
    JOBS_BY_SHEET[None] = []
    partial_results = [
        mk_result("p1", "s", None, None, [], mk_info("Lambda", "Eng", "Applied", "2026-01-01", confirmation=True), ms_for(2026, 1, 1)),
    ]
    groups = gb.group_results(partial_results)
    check("partial (cancelled-scan) results still group normally", len(groups) == 1 and groups[0]["email_count"] == 1)


# ── Case 13 (structural): scan_period never writes ──────────────────────────
def test_scan_period_is_read_only():
    # Require call syntax ("(") so this doesn't false-positive on scan_period's own
    # docstring, which names these functions in prose to explain why it never calls them.
    src = inspect.getsource(gb.scan_period)
    for forbidden in ("update_job_from_email(", "append_job(", "log_processed_email("):
        check(f"scan_period never calls {forbidden}", forbidden not in src)


# ── Rerun-safety at the pure-function level: same input -> same output ─────
def test_group_results_is_deterministic_across_reruns():
    JOBS_BY_SHEET[None] = []
    items = [mk_result("m1", "s", None, None, [], mk_info("Kappa", "Eng", "Applied", "2026-01-01", confirmation=True), ms_for(2026, 1, 1))]
    g1 = gb.group_results(items)
    g2 = gb.group_results(items)
    check("group_results on unchanged input is stable (no hidden re-analysis)", g1[0]["group_key"] == g2[0]["group_key"])


# ── Case 14: applying one approved group only touches that group's own emails ──
def test_apply_only_touches_approved_group():
    updates, appends, logged = [], [], []

    def fake_update(row_no, status, comments, email_date, sheet_name=None, email_info=None):
        updates.append((row_no, status))
        return True, []

    appmod.update_job_from_email = fake_update
    appmod.append_job = lambda data, sheet_name=None: (appends.append(data), 555)[1]
    gb.log_processed_email = lambda msg_id, *a, **k: logged.append(msg_id)

    JOBS_BY_SHEET[None] = [{
        "No.": "30", "Company": "Theta", "Role": "QA", "Status": "Applied",
        "Contact Person": "", "Company Comments": "", "Date Applied": "2026-01-01",
    }]
    matched_items = [
        mk_result("a1", "s", None, 30, [], mk_info("Theta", "QA", "Applied", "2026-01-01"), ms_for(2026, 1, 1)),
        mk_result("a2", "s", None, 30, [], mk_info("Theta", "QA", "Interview", "2026-01-10"), ms_for(2026, 1, 10)),
    ]
    group_a = gb._build_group("matched", ("row", None, 30), matched_items)

    new_items = [mk_result("b1", "s", None, None, [], mk_info("Iota", "Dev", "Applied", "2026-02-01", confirmation=True), ms_for(2026, 2, 1))]
    gb._build_group("new", ("new", None, "iota", "dev", "b1"), new_items)  # group B — never applied below

    result = gb.apply_group(group_a, {"comments": {}})
    check("applying group A succeeds", result["ok"] is True)
    check("applying group A writes exactly its own 2 rows, in order", updates == [(30, "Applied"), (30, "Interview")])
    check("applying group A never calls append_job", appends == [])
    check("applying group A only logs its own message ids", logged == ["a1", "a2"])
    check("group B's message id is never logged (untouched)", "b1" not in logged)


# ── Apply resilience: a Sheets-API rate-limit/transient error mid-batch used to crash
# the whole page (real incident). Verify a logging failure doesn't invalidate an
# already-successful sheet write, and that retrying a partially-applied batch skips
# whatever was already logged instead of writing duplicates. ──
def test_log_failure_does_not_fail_group():
    updates = []
    appmod.update_job_from_email = lambda row_no, status, comments, email_date, sheet_name=None, email_info=None: (updates.append((row_no, status)), (True, []))[1]

    def failing_log(msg_id, *a, **k):
        raise RuntimeError("simulated Sheets API error")

    gb.log_processed_email = failing_log
    JOBS_BY_SHEET[None] = [{
        "No.": "40", "Company": "Nu Corp", "Role": "Eng", "Status": "Applied",
        "Contact Person": "", "Company Comments": "", "Date Applied": "2026-01-01",
    }]
    items = [mk_result("l1", "s", None, 40, [], mk_info("Nu Corp", "Eng", "Interview", "2026-01-05"), ms_for(2026, 1, 5))]
    group = gb._build_group("matched", ("row", None, 40), items)
    result = gb.apply_group(group, {"comments": {}})
    check("group still succeeds when only the import-log write fails", result["ok"] is True)
    check("the actual sheet write happened despite the logging failure", updates == [(40, "Interview")])


def test_already_applied_messages_are_skipped_on_retry():
    updates, appends, logged = [], [], []
    appmod.update_job_from_email = lambda row_no, status, comments, email_date, sheet_name=None, email_info=None: (updates.append((row_no, status)), (True, []))[1]
    appmod.append_job = lambda data, sheet_name=None: (appends.append(data), 999)[1]
    gb.log_processed_email = lambda msg_id, *a, **k: logged.append(msg_id)

    JOBS_BY_SHEET[None] = [{
        "No.": "50", "Company": "Xi Co", "Role": "PM", "Status": "Applied",
        "Contact Person": "", "Company Comments": "", "Date Applied": "2026-01-01",
    }]
    items = [
        mk_result("r1", "s", None, 50, [], mk_info("Xi Co", "PM", "Applied", "2026-01-01"), ms_for(2026, 1, 1)),
        mk_result("r2", "s", None, 50, [], mk_info("Xi Co", "PM", "Interview", "2026-01-10"), ms_for(2026, 1, 10)),
    ]
    group = gb._build_group("matched", ("row", None, 50), items)
    result = gb.apply_group(group, {"comments": {}}, already_applied_ids={"r1"})
    check("retry skips the sheet write for an already-logged item", updates == [(50, "Interview")])
    check("retry only re-logs the not-yet-logged item", logged == ["r2"])
    check("the skipped item still counts toward applied", result["applied"] == 2)

    new_items = [mk_result("n1", "s", None, None, [], mk_info("Omicron", "Dev", "Applied", "2026-02-01", confirmation=True), ms_for(2026, 2, 1))]
    new_group = gb._build_group("new", ("new", None, "omicron", "dev", "n1"), new_items)
    result2 = gb.apply_group(new_group, {"comments": {}}, already_applied_ids={"n1"})
    check("an already-logged new-application group is skipped entirely, no duplicate row", result2.get("already_applied") is True and appends == [])


# ── Persistent login: session token issue/validate/expire/revoke round-trip ────
class _FakeSessionSheet:
    """Minimal in-memory stand-in for the hidden _app_sessions worksheet — just enough
    of the gspread Worksheet API (get_all_values/append_row/delete_rows) for
    _session_store_ws()'s callers to exercise the real persistence logic end-to-end."""

    def __init__(self):
        self.rows = [["token", "expires_at"]]

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def append_row(self, row, value_input_option=None):
        self.rows.append(list(row))

    def delete_rows(self, row_index):
        del self.rows[row_index - 1]


def test_session_token_issue_validate_expire_revoke():
    fake = _FakeSessionSheet()
    appmod._session_store_ws = lambda: fake

    token = appmod._issue_session_token()
    check("issuing a token adds exactly one row", len(fake.rows) == 2)
    tokens = appmod._load_valid_session_tokens()
    check("the issued token loads back with a future expiry", token in tokens and tokens[token] > datetime.now())

    appmod._revoke_session_token(token)
    check("revoking removes the row", len(fake.rows) == 1)
    check("a revoked token no longer loads", token not in appmod._load_valid_session_tokens())

    # An already-expired row must not be treated as valid, and issuing a new token
    # should prune it (best-effort cleanup) rather than let the sheet grow forever.
    fake.rows.append(["stale-token", "2020-01-01 00:00"])
    appmod._issue_session_token()
    remaining = [r[0] for r in fake.rows[1:]]
    check("issuing a new token prunes already-expired rows", "stale-token" not in remaining)


# ── Write-path resilience: a real incident left permanently blank rows because
# gspread's insert_row() does two separate API calls internally (open a blank row,
# then populate it) and a 429 on the second one after the first succeeded lost the
# values for good. Verify the reimplementation actually recovers. ──
import gspread as _gspread_for_tests  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def json(self):
        return {"error": {"code": self.status_code, "message": "simulated", "status": "RESOURCE_EXHAUSTED"}}


def _fake_api_error(status_code):
    return _gspread_for_tests.exceptions.APIError(_FakeResponse(status_code))


def test_with_sheets_retry_recovers_from_transient_errors_and_gives_up_on_others():
    calls = {"n": 0}

    def flaky_then_ok():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _fake_api_error(429)
        return "ok"

    result = appmod._with_sheets_retry(flaky_then_ok, max_attempts=5, base_delay=0.001)
    check("transient 429s are retried until success", result == "ok" and calls["n"] == 3)

    def always_permission_denied():
        raise _fake_api_error(403)

    raised = False
    try:
        appmod._with_sheets_retry(always_permission_denied, max_attempts=3)
    except _gspread_for_tests.exceptions.APIError:
        raised = True
    check("a non-retryable error (403) is not retried away, it propagates", raised)


class _FakeInsertWorksheet:
    """Simulates gspread's real insert_row() behavior: opening the row and writing its
    values are two separate calls. fail_step2_times controls how many times the second
    call rejects before succeeding, to prove the row ends up populated, not blank."""

    def __init__(self, fail_step2_times=0):
        self.fail_step2_times = fail_step2_times
        self.id = 999
        self.written = None
        self.spreadsheet = self

    def batch_update(self, body):
        return {"ok": True}  # step 1: opening the row always succeeds in this test

    def update(self, values, range_name, value_input_option=None):
        if self.fail_step2_times > 0:
            self.fail_step2_times -= 1
            raise _fake_api_error(429)
        self.written = (values, range_name)
        return {"ok": True}


def test_insert_row_with_values_recovers_when_populate_step_fails_first():
    real_sleep = appmod.time.sleep
    appmod.time.sleep = lambda s: None  # skip the real backoff delay for this test
    try:
        ws = _FakeInsertWorksheet(fail_step2_times=2)
        appmod._insert_row_with_values(ws, ["1", "2026-01-01 00:00", "Acme", "Role"], 5)
    finally:
        appmod.time.sleep = real_sleep
    check("the row is populated once the retried populate step finally succeeds", ws.written is not None)
    check("the values land in the exact row that was opened", ws.written[1] == "A5:D5")


def main():
    tests = [
        test_canonical_status_fixes_case_sensitivity,
        test_degenerate_comment_text_triggers_garbled_retry,
        test_single_email_one_group,
        test_matched_row_timeline_and_role_wording,
        test_same_company_different_roles_stay_separate,
        test_confirmation_then_later_rejection_becomes_one_new_group,
        test_two_separate_confirmations_same_company_role_stay_separate,
        test_close_together_confirmations_cluster_into_one_group,
        test_confirmations_beyond_cluster_window_stay_separate,
        test_apply_new_falls_back_to_internal_date_not_now,
        test_cross_year_confirmation_and_rejection_consolidate_into_2025,
        test_cross_year_fallback_bounded_to_jan_feb,
        test_jan_feb_2026_fallback_matches_2025_row,
        test_low_value_rejected_to_applied_skipped,
        test_low_value_applied_to_applied_filtered_unless_contact_backfill,
        test_low_value_new_group_with_blank_role_skipped,
        test_low_value_role_copied_from_subject_treated_as_blank,
        test_low_value_reason_is_human_readable_for_each_rule,
        test_ambiguous_never_merged,
        test_conflict_protection_kept,
        test_skip_before_llm,
        test_extraction_failure_continues_scan,
        test_chunked_scan_step_matches_whole_range_scan,
        test_cancel_partial_results_are_still_valid_groups,
        test_scan_period_is_read_only,
        test_group_results_is_deterministic_across_reruns,
        test_apply_only_touches_approved_group,
        test_log_failure_does_not_fail_group,
        test_already_applied_messages_are_skipped_on_retry,
        test_session_token_issue_validate_expire_revoke,
        test_with_sheets_retry_recovers_from_transient_errors_and_gives_up_on_others,
        test_insert_row_with_values_recovers_when_populate_step_fails_first,
    ]
    for t in tests:
        print(f"--- {t.__name__} ---")
        t()
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"All {sum(1 for _ in tests)} test functions passed.")


if __name__ == "__main__":
    main()
