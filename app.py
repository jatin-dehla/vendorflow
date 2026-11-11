"""VendorFlow - Automated Vendor Onboarding & Review (Streamlit UI).

Two screens only: Dashboard and New Vendor Review. All rules live in validation.py;
this file collects input, calls the pipeline and renders what it returns.

Display rule: any text that came from a user or from a PDF is passed through md_escape()
before it is shown, so it is always displayed literally. The few HTML snippets used here
(status pills, header) are built only from fixed strings.
"""
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import streamlit as st

import storage
import validation as v
from sample_data import ALL_SLOTS, NEGATIVE_EXAMPLES, SAMPLES, sample_docs_for

DOCS_DIR = Path(__file__).with_name("sample_docs")
PAGES = ("Dashboard", "New Vendor Review")
SAMPLE_BY_LABEL = {s["label"]: s for s in SAMPLES}
FORM_KEYS = {  # form field -> session_state key
    "legal_name": "f_legal_name", "contact_email": "f_contact_email", "country": "f_country",
    "tax_id": "f_tax_id", "bank_name": "f_bank_name", "account_holder": "f_account_holder",
    "account_last4": "f_account_last4",
}
STAGE_TITLES = {  # display names only; statuses always come from the run record
    "1": "Receive submission",
    "2": "Check documents",
    "3": "Validate tax information",
    "4": "Check banking consistency",
    "5": "Check policy and duplicates",
    "6": "Make and record decision",
}
PILL_LABELS = {"PASS", "FLAG", "SKIP", "ERROR", "NOT RUN", "APPROVED", "PENDING", "REJECTED", "RECORDED"}
STATUS_ICON = {"APPROVED": "🟢", "PENDING": "🟠", "REJECTED": "🔴"}
DECISION_MEANING = {
    "APPROVED": "Passed the configured screening checks",
    "PENDING": "Needs correction or manual review",
    "REJECTED": "Declined under internal demo policy",
    "ERROR": "Technical failure - no decision was made",
}

st.set_page_config(page_title="VendorFlow", layout="wide")
st.markdown(
    """
    <style>
    /* Top padding clears Streamlit's fixed toolbar (about 3.75rem) together with the header's own padding. */
    .block-container {max-width: 1400px; padding: 3rem clamp(1rem, 3vw, 2.5rem) 1.5rem clamp(1rem, 3vw, 2.5rem);}
    .vf-header {display: flex; flex-wrap: wrap; align-items: baseline; column-gap: 0.9rem; row-gap: 0.1rem; padding: 0.8rem 0 0.4rem 0;}
    .vf-title {font-size: 1.9rem; line-height: 1.35; font-weight: 700; color: #2546F5;}
    .vf-tagline {font-size: 1rem; line-height: 1.4; color: #111827;}
    .vf-case {font-size: 0.75rem; line-height: 1.4; color: #6B7280; margin-left: auto;}
    .vf-pill {display: inline-block; padding: 0.04rem 0.55rem; border-radius: 999px; border: 1px solid; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.02em; line-height: 1.5; vertical-align: middle;}
    .vf-big {font-size: 0.9rem; padding: 0.15rem 0.8rem;}
    .vf-PASS, .vf-APPROVED, .vf-RECORDED {background: #DCFCE7; color: #166534; border-color: #86EFAC;}
    .vf-FLAG, .vf-PENDING {background: #FEF3C7; color: #92400E; border-color: #FCD34D;}
    .vf-REJECTED, .vf-ERROR {background: #FEE2E2; color: #991B1B; border-color: #FCA5A5;}
    .vf-SKIP, .vf-NOTRUN {background: #F1F5F9; color: #475569; border-color: #CBD5E1;}
    .vf-meta {color: #6B7280; font-size: 0.85rem;}
    .vf-footer {color: #6B7280; font-size: 0.8rem;}
    [data-testid="stMetric"] {border: 1px solid #E5E7EB; border-radius: 10px; padding: 0.6rem 0.9rem; background: #FFFFFF;}
    [data-testid="stForm"] [data-testid="stVerticalBlock"] {gap: 0.6rem;}
    </style>
    """,
    unsafe_allow_html=True,
)
DASHBOARD_GRADIENT = (
    "<style>.stApp {background-image: linear-gradient(180deg, #EFF6FF 0, #FFFFFF 340px); "
    "background-repeat: no-repeat;}</style>"
)

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~$<&:])")


# ------------------------------------------------------------------ small helpers
def md_escape(text):
    """Escape Markdown so user- or PDF-supplied text is shown literally."""
    return _MD_SPECIAL.sub(r"\\\1", str(text or ""))


_ID_LIKE = re.compile(r"\b(?:\d{2}[A-Z0-9]{13}|\d{2}-?\d{7})\b")  # GSTIN-like or EIN-like tokens


def mask_ids(text):
    """History views (shared on the public demo) show tax identifiers masked to their last four characters."""
    return _ID_LIKE.sub(lambda m: v.mask_tax_id(m.group(0)), text or "")


def pill(label, big=False):
    """Status pill from a fixed vocabulary (never from user text); the word is always shown."""
    label = label if label in PILL_LABELS else "SKIP"
    return f'<span class="vf-pill vf-{label.replace(" ", "")}{" vf-big" if big else ""}">{label}</span>'


def fmt_time(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return iso


def goto(page):
    if st.session_state.get("page") != page:
        # Form widgets are discarded when their screen is left, so a result from
        # an earlier run must not linger next to a blank form.
        st.session_state.pop("last_record", None)
        st.session_state.pop("opened_run", None)
    st.session_state["page"] = page


def short_issue(decision, summary):
    if decision == "APPROVED":
        return "All configured checks passed"
    summary = summary or ""
    return summary if len(summary) <= 100 else summary[:97] + "..."


def draft_text(msg):
    return f"Draft - not sent\n\nTo: {msg['to']}\nSubject: {msg['subject']}\n\n{msg['body']}\n"


def email_markdown(body):
    """Escaped body; blank lines stay paragraph breaks, single newlines stay line breaks."""
    return "\n\n".join(md_escape(p).replace("\n", "  \n") for p in body.split("\n\n"))


# ------------------------------------------------------------------ processing trace
def stage_number(stage):
    return stage["name"].split(".")[0]


def stage_summary(stage):
    """(pill label, short text) for one stage dict, derived from its real results."""
    n = stage_number(stage)
    if stage["status"] == "ERROR":
        return "ERROR", stage["detail"]
    if n == "6":
        return "RECORDED", stage["detail"].replace("Decision:", "Decision recorded —")
    counts = Counter(c["status"] for c in stage["checks"])
    parts = [f"{counts['PASS']} passed"]
    if counts["FLAG"]:
        parts.append(f"{counts['FLAG']} flagged")
    if counts["SKIP"]:
        parts.append(f"{counts['SKIP']} not evaluated")
    return stage["status"], " · ".join(parts)


def render_stage(stage, mask=False):
    """One stage: a header line plus every check with its explanation. Used live (on_stage)
    and when re-displaying a stored run, so both look identical. mask=True for history views."""
    hide = mask_ids if mask else str
    n = stage_number(stage)
    label, text = stage_summary(stage)
    lines = [f"{pill(label)} **{n}. {STAGE_TITLES.get(n, md_escape(stage['name']))}** · {md_escape(hide(text))}"]
    for c in stage["checks"]:
        lines.append(f"- {pill(c['status'])} **{md_escape(c['label'])}** - {md_escape(hide(c['detail']))}")
    st.markdown("\n".join(lines), unsafe_allow_html=True)


def render_trace(rec, mask=False):
    by_number = {stage_number(s): s for s in rec["stages"]}
    for n, title in STAGE_TITLES.items():
        if n in by_number:
            render_stage(by_number[n], mask)
        else:  # never reached because an earlier stage failed technically
            st.markdown(f"{pill('NOT RUN')} **{n}. {title}** · not reached", unsafe_allow_html=True)


def trace_label(rec):
    by_number = {stage_number(s): s for s in rec["stages"]}
    flagged = sum(1 for s in rec["stages"] for c in s["checks"] if c["status"] == "FLAG")
    if rec["execution_status"] == "ERROR":
        done = sum(1 for n in STAGE_TITLES if n in by_number and by_number[n]["status"] != "ERROR")
        return f"Processing trace - stopped by a technical error ({done} of 6 stages completed)"
    flags = f"{flagged} check{'s' if flagged != 1 else ''} flagged" if flagged else "no checks flagged"
    return f"Processing trace - 6 of 6 stages completed · {flags}"


# ------------------------------------------------------------------ email preview
def render_email(msg, run_id, scope):
    st.markdown('<span class="vf-pill vf-FLAG">Draft — not sent</span>', unsafe_allow_html=True)
    to_text = md_escape(msg["to"]) if msg["to"] else "*no valid contact email - add a recipient manually*"
    st.markdown(f"**To:** {to_text}  \n**Subject:** {md_escape(msg['subject'])}")
    with st.container(height=320, border=True):  # scrolls inside when the email is long
        st.markdown(email_markdown(msg["body"]))
    st.download_button("Download draft (.txt)", data=draft_text(msg), type="primary",
                       file_name=f"vendor_email_draft_{run_id[:8]}.txt", mime="text/plain",
                       key=f"dl_{scope}_{run_id}")
    with st.expander("Copy draft"):
        st.code(draft_text(msg), language=None, wrap_lines=True)
        st.caption("Use the copy icon at the top right of the box.")
    st.caption("VendorFlow never sends email. This is a generated draft for you to review and send yourself.")


@st.dialog("Vendor email preview", width="large")
def email_dialog(msg, run_id):
    render_email(msg, run_id, "email")


# ------------------------------------------------------------------ result card
def render_submitted(rec):
    vendor = rec["vendor"]
    lines = [
        f"- **Legal name:** {md_escape(vendor['legal_name']) or '(empty)'}",
        f"- **Contact email:** {md_escape(vendor['contact_email']) or '(empty)'}",
        f"- **Country:** {md_escape(vendor['country']) or '(empty)'}",
        f"- **Tax ID (masked):** {md_escape(vendor['masked_tax_id']) or '(empty)'}",
        f"- **Bank:** {md_escape(vendor['bank_name']) or '(empty)'}, account ending "
        f"{md_escape(vendor['account_last4']) or '(empty)'}",
        f"- **Account holder:** {md_escape(vendor['account_holder']) or '(empty)'}",
    ]
    for a in rec["attachments"] or []:
        lines.append(f"- **{md_escape(a['document_type'])}:** {md_escape(a['filename'])} ({a['size_bytes']:,} bytes)")
    if not rec["attachments"]:
        lines.append("- **Documents:** none attached")
    st.markdown("\n".join(lines))


def render_reasons_and_actions(rec, limit=None, mask=False):
    hide = mask_ids if mask else str
    for title, items in (("Main reasons", rec["internal_reasons"]), ("Recommended actions", rec["next_actions"])):
        shown = items if limit is None else items[:limit]
        st.markdown(f"**{title}**\n" + "\n".join(f"- {md_escape(hide(i))}" for i in shown))
        if len(shown) < len(items):
            st.caption(f"...and {len(items) - len(shown)} more. See the processing trace for every check.")


def render_follow_up(rec):
    msg = rec["vendor_message"]
    st.markdown("**Vendor follow-up**")
    if msg["state"] != "DRAFT":
        if rec["execution_status"] == "SUCCESS":
            st.markdown("No follow-up required.")
        return
    summary = "Rejection notice prepared" if rec["decision"] == "REJECTED" else "Correction request prepared"
    to_text = md_escape(msg["to"]) if msg["to"] else "no valid contact email"
    st.markdown(f"Draft prepared - not sent · {summary}  \n**Recipient:** {to_text}")
    if st.button("Preview email", type="primary", key=f"preview_{rec['run_id']}"):
        email_dialog(msg, rec["run_id"])


def render_result(rec):
    """Final decision, main reasons, actions, compact submitted details and follow-up.
    The stage-by-stage detail lives only in the processing trace, not here."""
    ok = rec["execution_status"] == "SUCCESS"
    label = rec["decision"] if ok else "ERROR"
    with st.container(border=True):
        name = md_escape(rec["vendor"]["legal_name"]) or "(no company name)"
        st.markdown(
            f"**{name}**  \n{pill(label, big=True)} {DECISION_MEANING[label]} "
            f'<span class="vf-meta">· Run {rec["run_id"][:8]} · {fmt_time(rec["submitted_at_utc"])}</span>',
            unsafe_allow_html=True)
        if not ok:
            st.error("The run did not complete, so no decision was made or recorded.")
        render_reasons_and_actions(rec, limit=5)
        with st.expander("Submitted details"):
            render_submitted(rec)
        render_follow_up(rec)


# ------------------------------------------------------------------ dialogs
@st.dialog("Run details", width="large")
def run_dialog(rec):
    vendor = rec["vendor"]
    label = rec["decision"] if rec["execution_status"] == "SUCCESS" else "ERROR"
    st.markdown(f"#### {md_escape(vendor['legal_name']) or '(no company name)'}")
    st.markdown(f'{pill(label, big=True)} <span class="vf-meta">Run {rec["run_id"][:8]} · '
                f'{fmt_time(rec["submitted_at_utc"])}</span>', unsafe_allow_html=True)
    has_email = rec["vendor_message"]["state"] == "DRAFT"
    tabs = st.tabs(["Summary", "Processing trace"] + (["Email draft"] if has_email else []))
    with tabs[0]:
        st.markdown("**Submitted details**")
        render_submitted(rec)
        render_reasons_and_actions(rec, mask=True)
    with tabs[1]:
        render_trace(rec, mask=True)
    if has_email:
        with tabs[2]:
            render_email(rec["vendor_message"], rec["run_id"], "history")


@st.dialog("Reset demo history?")
def reset_dialog():
    st.warning("This permanently deletes ALL stored demo runs, for every visitor of this demo. "
               "The fictional internal denylist is not affected.")
    cancel, confirm = st.columns(2)
    if cancel.button("Cancel", key="reset_cancel"):
        st.rerun()
    if confirm.button("Confirm reset", type="primary", key="reset_confirm"):
        do_reset()
        st.rerun()


@st.dialog("Demo disclaimer")
def disclaimer_dialog():
    st.markdown(
        "- **Synthetic data only.** Use fictional companies and the sample documents. Please do not enter real data.\n"
        "- **Independent case-study application.** Built by the candidate for Zamp's ASA case study (PS-2). "
        "It is not an official Zamp product and is not endorsed by Zamp.\n"
        "- **Tax IDs** are checked for format only. This does not confirm any government registration.\n"
        "- **Documents:** VendorFlow reads the text of machine-readable PDFs and compares it with the form. "
        "A match does not establish authenticity. Images and scanned or unreadable files need manual verification.\n"
        "- **Bank details:** name and digit comparisons only. Bank ownership is not verified.\n"
        "- **The denylist** is a fictional internal list, not real sanctions screening.\n"
        "- **Approved** means the submission passed these demo rules. It is not KYC or legal verification.\n"
        "- **Email drafts** are never sent by this app.\n"
        "- **Storage:** uploaded files are read in memory and not saved. Run history (details and decisions) "
        "is kept in SQLite, which on free cloud hosting may be shared between visitors and can reset.")


def do_reset():
    try:
        storage.clear_all()
        st.session_state["flash"] = "Demo history cleared."
    except Exception as exc:
        st.session_state["flash_error"] = f"Could not clear history ({type(exc).__name__})."
    st.session_state.pop("opened_run", None)
    st.session_state["tbl_ver"] = st.session_state.get("tbl_ver", 0) + 1  # clears any row selection


# ------------------------------------------------------------------ Dashboard
def dashboard():
    st.markdown(DASHBOARD_GRADIENT, unsafe_allow_html=True)
    st.caption("Review submissions, see rule-based decisions, and track follow-ups.")
    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))
    if st.session_state.get("flash_error"):
        st.error(st.session_state.pop("flash_error"))

    try:
        c = storage.counts()
        runs = storage.list_runs()
    except Exception as exc:
        st.error(f"Run history could not be read ({type(exc).__name__}). Please retry.")
        return

    cols = st.columns([1, 1, 1, 1, 1.4])
    cols[0].metric("Total submissions", c["total"])
    cols[1].metric("Approved", c["approved"])
    cols[2].metric("Pending", c["pending"])
    cols[3].metric("Rejected", c["rejected"])
    cols[4].button("New vendor review", type="primary", on_click=goto, args=("New Vendor Review",))

    head, reset = st.columns([5, 1.3])
    head.subheader("Recent submissions")
    if reset.button("Reset history", key="reset_open", icon=":material/delete:", disabled=not runs):
        reset_dialog()

    if not runs:
        st.info("No history yet. Start a new vendor review to create the first real record.")
        return

    rows = [{"Vendor": r["legal_name"] or "(no name)", "Submitted": fmt_time(r["submitted_at_utc"]),
             "Country": r["country"] or "-", "Status": f"{STATUS_ICON[r['decision']]} {r['decision']}",
             "Main issue": short_issue(r["decision"], mask_ids(r["summary"]))} for r in runs]
    event = st.dataframe(
        rows, hide_index=True, on_select="rerun", selection_mode="single-row",
        key=f"runs_table_{st.session_state.get('tbl_ver', 0)}",
        column_config={"Vendor": st.column_config.TextColumn(width="medium"),
                       "Submitted": st.column_config.TextColumn(width="medium"),
                       "Country": st.column_config.TextColumn(width="small"),
                       "Status": st.column_config.TextColumn(width="small"),
                       "Main issue": st.column_config.TextColumn(width="large")})
    st.caption("Select a row to view that run's details. Click the selected row again to deselect it.")

    selected = event.selection.rows
    if selected and selected[0] < len(runs):
        run_id = runs[selected[0]]["run_id"]
        if st.session_state.get("opened_run") != run_id:  # open once per selection, not on every rerun
            st.session_state["opened_run"] = run_id
            rec = storage.get_run(run_id)
            if rec:
                run_dialog(rec)
    else:
        st.session_state.pop("opened_run", None)


# ------------------------------------------------------------------ New Vendor Review
def apply_sample():
    choice = st.session_state.get("sample_choice")
    sample = SAMPLE_BY_LABEL.get(choice)
    fields = sample["fields"] if sample else {k: "" for k in FORM_KEYS}
    for field, key in FORM_KEYS.items():
        st.session_state[key] = fields[field] if field != "country" or sample else "India"
    # A sample switch starts a clean attempt: drop the old result and old uploads.
    # Uploader values cannot be assigned, so a new key version gives empty uploaders.
    st.session_state.pop("last_record", None)
    st.session_state["upload_ver"] = st.session_state.get("upload_ver", 0) + 1
    if sample:
        missing = [v.DOC_LABELS[s] for s in ALL_SLOTS if s not in sample["attach"]]
        note = "Download this scenario's documents below and attach them in the form." if not missing else \
            f"Attach this scenario's documents below, leaving {', '.join(missing)} empty on purpose."
        st.session_state["sample_note"] = note
    else:
        st.session_state.pop("sample_note", None)


def to_upload(f):
    # The bytes are read in memory for this run only and are never stored.
    return v.Upload(f.name, f.size, f.type or "", f.getvalue()) if f is not None else None


def sample_documents_panel():
    sample = SAMPLE_BY_LABEL.get(st.session_state.get("sample_choice"))
    with st.expander("Sample documents (fictional PDFs) to download and attach"):
        if sample is None:
            st.caption("Choose a sample above to download the documents that belong to it.")
        else:
            for slot, rel in sample_docs_for(sample).items():
                path = DOCS_DIR / rel
                if path.exists():
                    st.download_button(f"{v.DOC_LABELS[slot]}: {path.name}", path.read_bytes(),
                                       file_name=path.name, mime="application/pdf", key=f"sd_{sample['id']}_{slot}")
        st.caption("Mismatch examples: use with the Northstar sample (H1), or put a registration proof "
                   "in the Bank proof slot.")
        for rel, description in NEGATIVE_EXAMPLES.items():
            path = DOCS_DIR / rel
            if path.exists():
                st.download_button(f"{description}", path.read_bytes(), file_name=path.name,
                                   mime="application/pdf", key=f"neg_{path.stem}")


def review_page():
    st.caption("Enter a vendor submission, run the checks, and review the decision. "
               "Demo: use fictional company information and sample documents only.")
    st.session_state.setdefault("f_country", "India")
    ver = st.session_state.setdefault("upload_ver", 0)  # bumped by apply_sample to reset uploaders
    doc_types = ["pdf", "png", "jpg", "jpeg"]

    left, right = st.columns([1.3, 1], gap="large")
    with left:
        st.selectbox("Load sample (optional - fills the form fields only)",
                     ["None"] + list(SAMPLE_BY_LABEL), key="sample_choice", on_change=apply_sample)
        if st.session_state.get("sample_note"):
            st.caption(st.session_state["sample_note"])
        sample_documents_panel()

        with st.form("review_form"):
            st.markdown("**Company**")
            legal_name = st.text_input("Legal company name", key="f_legal_name")
            col_email, col_country = st.columns(2)
            contact_email = col_email.text_input("Contact email", key="f_contact_email")
            country = col_country.selectbox("Country", v.COUNTRIES, key="f_country")
            tax_id = st.text_input("Tax ID (GSTIN for India, EIN for United States)", key="f_tax_id")
            st.markdown("**Banking**")
            col_bank, col_last4 = st.columns(2)
            bank_name = col_bank.text_input("Bank name", key="f_bank_name")
            account_last4 = col_last4.text_input("Account number - last 4 digits only",
                                                 key="f_account_last4", max_chars=4)
            account_holder = st.text_input("Account holder name", key="f_account_holder")
            st.markdown("**Supporting documents**")
            st.caption("PDF, PNG or JPG, max 5 MB each. Text-based PDFs are read and compared with the form; "
                       "images and scans need manual verification.")
            col_reg, col_tax, col_bankdoc = st.columns(3)
            up_reg = col_reg.file_uploader("Registration proof", type=doc_types, key=f"u_reg_{ver}")
            up_tax = col_tax.file_uploader("Tax proof", type=doc_types, key=f"u_tax_{ver}")
            up_bank = col_bankdoc.file_uploader("Bank proof", type=doc_types, key=f"u_bank_{ver}")
            submitted = st.form_submit_button("Run checks", type="primary")

    with right:
        st.markdown("##### Live processing trace")
        trace_slot = st.container()
        if submitted:
            sub = v.Submission(
                legal_name=legal_name, contact_email=contact_email, country=country, tax_id=tax_id,
                bank_name=bank_name, account_holder=account_holder, account_last4=account_last4,
                documents={"registration_proof": to_upload(up_reg), "tax_proof": to_upload(up_tax),
                           "bank_proof": to_upload(up_bank)})
            with trace_slot:
                with st.status("Running checks...", expanded=True) as status:
                    # each stage is drawn right after that stage really runs
                    record = v.process_submission(sub, find_prior=storage.find_prior_identity,
                                                  save=storage.save_run, on_stage=render_stage)
                    status.update(label=trace_label(record), expanded=False,
                                  state="error" if record["execution_status"] == "ERROR" else "complete")
            st.session_state["last_record"] = record

        rec = st.session_state.get("last_record")
        if rec is None:
            with trace_slot:
                st.info("No result yet. Fill in the form (or load a sample) and click Run checks.")
        else:
            if not submitted:  # the live status above already holds the trace for a fresh run
                with trace_slot:
                    with st.expander(trace_label(rec)):
                        render_trace(rec)
            st.markdown("##### Result")
            render_result(rec)
            if rec["execution_status"] == "SUCCESS":
                st.button("View run history", on_click=goto, args=("Dashboard",), key="to_hist")


# ------------------------------------------------------------------ footer + main
def footer():
    st.divider()
    left, right = st.columns([5, 1.3])
    left.markdown('<span class="vf-footer">© 2026 Jatin Choudhary · VendorFlow · Zamp ASA Case Study, PS-2</span>',
                  unsafe_allow_html=True)
    if right.button("Demo disclaimer", key="disclaimer_open"):
        disclaimer_dialog()


def main():
    st.session_state.setdefault("page", "Dashboard")
    try:
        storage.init_db()
    except Exception as exc:
        st.error(f"Run history storage is unavailable ({type(exc).__name__}). Checks cannot be saved.")

    # Static text only (no user input), so unsafe_allow_html is not an injection risk here.
    st.markdown('<div class="vf-header"><span class="vf-title">VendorFlow</span>'
                '<span class="vf-tagline">Automated Vendor Onboarding &amp; Review</span>'
                '<span class="vf-case">Zamp ASA Case Study · PS-2</span></div>',
                unsafe_allow_html=True)
    nav_cols = st.columns([1.1, 1.6, 4])
    for col, name in zip(nav_cols, PAGES):  # selected screen is the filled (primary) button
        col.button(name, key=f"nav_{name}", on_click=goto, args=(name,),
                   type="primary" if st.session_state["page"] == name else "secondary")
    st.divider()
    if st.session_state["page"] == "Dashboard":
        dashboard()
    else:
        review_page()
    footer()


main()
