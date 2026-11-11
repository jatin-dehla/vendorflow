"""Tests for the VendorFlow pipeline. Every scenario goes through the real
process_submission(), real PDF text extraction (pypdf) and a real temporary SQLite file;
nothing is mocked except the deliberately failing save in A9 and the missing-pypdf case.

Documents come from two places: PDFs generated to match whatever the form says
(sample_data.document_pdfs), or the actual files in sample_docs/ ("disk" tests)."""
import json
import os
from pathlib import Path

import pytest

import storage
import validation as v
from sample_data import (ALL_SLOTS, NEGATIVE_EXAMPLES, SAMPLES, bank_pdf, build_pdf, document_pdfs,
                         registration_pdf, sample_docs_for, tax_pdf)

SAMPLE_DOCS = Path(__file__).resolve().parent.parent / "sample_docs"
SAMPLE_BY_ID = {s["id"]: s for s in SAMPLES}
H1 = SAMPLE_BY_ID["H1"]["fields"]


def pdf_upload(name, data, size=None):
    return v.Upload(name, len(data) if size is None else size, "application/pdf", data)


def disk_upload(relative):
    path = SAMPLE_DOCS / relative
    data = path.read_bytes()
    return v.Upload(path.name, len(data), "application/pdf", data)


def make_sub(fields, attach=ALL_SLOTS, doc_fields=None, **doc_overrides):
    pdfs = document_pdfs(doc_fields or fields, attach) if attach else {}
    docs = {slot: (pdf_upload(f"{slot}.pdf", pdfs[slot]) if slot in pdfs else None) for slot in ALL_SLOTS}
    docs.update(doc_overrides)
    return v.Submission(documents=docs, **fields)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    storage.init_db(path)
    return path


def process(db, sub, save=True):
    return v.process_submission(
        sub,
        find_prior=lambda c, t: storage.find_prior_identity(c, t, db),
        save=(lambda r: storage.save_run(r, db)) if save else None,
    )


def run(db, fields, attach=ALL_SLOTS, save=True, doc_fields=None, **doc_overrides):
    return process(db, make_sub(fields, attach, doc_fields, **doc_overrides), save)


def sample_run(db, sample_id, **overrides):
    """Form = sample fields + overrides. Documents state the sample's values with the same
    overrides applied; an override of "" means the field was left blank on the form only."""
    s = SAMPLE_BY_ID[sample_id]
    fields = {**s["fields"], **overrides}
    doc_fields = {**s["fields"], **{k: x for k, x in overrides.items() if x != ""}}
    return run(db, fields, s["attach"], doc_fields=doc_fields)


def disk_run(db, sample_id, **overrides):
    """Run a scenario using the ACTUAL PDF files in sample_docs/."""
    s = SAMPLE_BY_ID[sample_id]
    docs = {slot: disk_upload(rel) for slot, rel in sample_docs_for(s).items()}
    sub = v.Submission(documents={slot: docs.get(slot) for slot in ALL_SLOTS}, **{**s["fields"], **overrides})
    return process(db, sub)


def reasons(rec):
    return " | ".join(rec["internal_reasons"])


def stage(rec, prefix):
    return next(s for s in rec["stages"] if s["name"].startswith(prefix))


def check(rec, label):
    return next(c for c in stage(rec, "2.")["checks"] if c["label"] == label)


# ------------------------------------------------------------ formal cases
def test_h1_happy_path_approved(db):
    rec = sample_run(db, "H1")
    assert rec["decision"] == "APPROVED" and rec["execution_status"] == "SUCCESS"
    assert rec["vendor_message"]["state"] == "NOT_REQUIRED"
    assert "not identity" in reasons(rec)
    assert len(rec["stages"]) == 6
    assert storage.counts(db) == {"total": 1, "approved": 1, "pending": 0, "rejected": 0}


def test_e1_missing_tax_proof_pending(db):
    rec = sample_run(db, "E1")
    assert rec["decision"] == "PENDING"
    assert "Tax proof missing" in reasons(rec)
    assert "tax registration document" in rec["vendor_message"]["body"]
    assert rec["vendor_message"]["state"] == "DRAFT"
    assert stage(rec, "2.")["status"] == "FLAG"


def test_e2_holder_mismatch_pending_non_accusatory(db):
    rec = sample_run(db, "E2")
    assert rec["decision"] == "PENDING"
    assert "does not match" in reasons(rec)
    body = rec["vendor_message"]["body"].lower()
    assert "manual review" in body
    assert "fraud" not in body and "fraud" not in reasons(rec).lower()


def test_e3_us_vendor_with_india_tax_id_pending(db):
    rec = sample_run(db, "E3")
    assert rec["decision"] == "PENDING"
    assert "United States format" in reasons(rec)
    assert "XX-XXXXXXX" in rec["vendor_message"]["body"]


def test_e4_denylist_rejected_with_discreet_vendor_message(db):
    rec = sample_run(db, "E4")
    assert rec["decision"] == "REJECTED"
    assert "synthetic internal denylist" in reasons(rec)
    msg = rec["vendor_message"]
    assert msg["state"] == "DRAFT"
    text = (msg["subject"] + msg["body"]).lower()
    assert "denylist" not in text and "33block" not in text and "internal onboarding policy" in text


# ------------------------------------------------------------ additional A1-A10
def test_a1_duplicate_after_approved(db):
    first = sample_run(db, "H1")
    second = sample_run(db, "H1", legal_name="Another Name Pvt Ltd", account_holder="Another Name Pvt Ltd")
    assert second["decision"] == "PENDING"
    assert first["run_id"][:8] in reasons(second)
    assert "duplicate" in reasons(second).lower()
    assert storage.counts(db)["total"] == 2


def test_a1b_duplicate_also_against_earlier_pending(db):
    sample_run(db, "E2")
    again = sample_run(db, "E2")
    assert again["decision"] == "PENDING" and "duplicate" in reasons(again).lower()


def test_a2_us_ein_hyphen_variation_still_duplicate(db):
    assert sample_run(db, "A8")["decision"] == "APPROVED"
    second = sample_run(db, "A8", tax_id="123456789")
    assert second["decision"] == "PENDING" and "duplicate" in reasons(second).lower()


def test_a3_denylist_beats_missing_proof(db):
    rec = sample_run(db, "E4")
    rec = run(db, SAMPLE_BY_ID["E4"]["fields"], attach=("registration_proof", "tax_proof"))
    assert rec["decision"] == "REJECTED"
    assert "synthetic internal denylist" in reasons(rec)
    assert "Bank proof missing" in reasons(rec)


def test_a4_missing_legal_name_skips_comparison(db):
    rec = sample_run(db, "H1", legal_name="")
    assert rec["decision"] == "PENDING"
    assert "Legal company name is missing" in reasons(rec)
    banking = stage(rec, "4.")
    cmp_check = next(c for c in banking["checks"] if c["label"] == "Holder vs legal name")
    assert cmp_check["status"] == "SKIP"
    assert "does not match" not in reasons(rec)


def test_a5_invalid_email_pending(db):
    rec = sample_run(db, "H1", contact_email="invalid-email")
    assert rec["decision"] == "PENDING" and "not a valid email" in reasons(rec)
    assert rec["vendor_message"]["to"] == ""


def test_a6_unsupported_and_oversized_files(db):
    bad_type = run(db, SAMPLE_BY_ID["H1"]["fields"], bank_proof=v.Upload("bank.exe", 1000, "application/x-msdownload"))
    assert bad_type["decision"] == "PENDING" and "unsupported type" in reasons(bad_type)
    big = run(db, {**SAMPLE_BY_ID["H1"]["fields"], "tax_id": "29PQRST5678K1Z8"},
              tax_proof=v.Upload("tax.pdf", v.MAX_FILE_BYTES + 1, "application/pdf"))
    assert big["decision"] == "PENDING" and "larger than 5 MB" in reasons(big)
    edge_fields = {**SAMPLE_BY_ID["H1"]["fields"], "tax_id": "24AAAAA1111A1Z1"}
    ok_edge = run(db, edge_fields, tax_proof=pdf_upload("tax.pdf", document_pdfs(edge_fields)["tax_proof"],
                                                        size=v.MAX_FILE_BYTES))
    assert ok_edge["decision"] == "APPROVED"  # exactly 5 MB is allowed


def test_a7_name_normalization_no_false_mismatch(db):
    rec = sample_run(db, "H1", legal_name="Northstar Office Supplies Pvt. Ltd.",
                     account_holder="northstar  office supplies pvt ltd", tax_id="24AAAAA1111A1Z1")
    assert rec["decision"] == "APPROVED"


def test_a7b_no_alias_stripping(db):
    rec = sample_run(db, "H1", account_holder="Northstar Office Supplies", tax_id="24AAAAA1111A1Z1")
    assert rec["decision"] == "PENDING" and "does not match" in reasons(rec)


def test_a8_us_happy_path(db):
    assert sample_run(db, "A8")["decision"] == "APPROVED"


def test_a9_save_failure_is_error_not_decision(db):
    def boom(record):
        raise RuntimeError("disk full")
    sub = make_sub(SAMPLE_BY_ID["H1"]["fields"])
    rec = v.process_submission(sub, find_prior=lambda c, t: None, save=boom)
    assert rec["execution_status"] == "ERROR" and rec["decision"] is None
    assert "could not be saved" in reasons(rec)
    assert rec["stages"][-1]["status"] == "ERROR"
    assert storage.counts(db)["total"] == 0


def test_a9b_stage_exception_is_error(db):
    def broken(country, tax):
        raise sqlite_error()
    sub = make_sub(SAMPLE_BY_ID["H1"]["fields"])
    rec = v.process_submission(sub, find_prior=broken, save=None)
    assert rec["execution_status"] == "ERROR" and rec["decision"] is None


def sqlite_error():
    import sqlite3
    return sqlite3.OperationalError("database is locked")


def test_a10_reset_then_h1_again_and_denylist_still_applies(db):
    sample_run(db, "H1")
    storage.clear_all(db)
    assert storage.counts(db) == {"total": 0, "approved": 0, "pending": 0, "rejected": 0}
    assert sample_run(db, "H1")["decision"] == "APPROVED"
    assert sample_run(db, "E4")["decision"] == "REJECTED"


# ------------------------------------------------------------ extra rules
def test_denylist_is_country_specific(db):
    rec = sample_run(db, "H1", country="United States", tax_id="33BLOCK1234X1Z9")
    assert "denylist" not in reasons(rec).lower()  # fails US format instead
    assert rec["decision"] == "PENDING"


def test_india_id_must_not_pass_as_us_and_vice_versa():
    assert v.tax_format_ok("India", "27ABCDE1234F1Z5")
    assert not v.tax_format_ok("United States", "27ABCDE1234F1Z5")
    assert v.tax_format_ok("United States", "12-3456789") and v.tax_format_ok("United States", "123456789")
    assert not v.tax_format_ok("India", "123456789")


def test_lowercase_tax_id_is_normalized(db):
    assert sample_run(db, "H1", tax_id="  27abcde1234f1z5 ")["decision"] == "APPROVED"


def test_last4_must_be_four_digits(db):
    rec = sample_run(db, "H1", account_last4="12345")
    assert rec["decision"] == "PENDING" and "four digits" in reasons(rec)


def test_blank_submission_is_pending_never_approved(db):
    rec = run(db, {}, attach=())
    assert rec["decision"] == "PENDING"
    assert stage(rec, "3.")["status"] == "SKIP" and stage(rec, "5.")["status"] == "SKIP"


def test_arbitrary_new_vendor_not_from_samples(db):
    rec = run(db, dict(legal_name="Harbor & Pine Traders Ltd.", contact_email="ap@harborpine.example",
                       country="India", tax_id="07HPTLD4321M1Z2", bank_name="Demo Bank",
                       account_holder="HARBOR PINE TRADERS LTD", account_last4="9034"))
    assert rec["decision"] == "APPROVED"


def test_on_stage_called_once_per_stage_in_order(db):
    seen = []
    v.process_submission(make_sub(SAMPLE_BY_ID["H1"]["fields"]), find_prior=lambda c, t: None,
                         save=None, on_stage=lambda s: seen.append(s["name"]))
    assert [n[:2] for n in seen] == ["1.", "2.", "3.", "4.", "5.", "6."]


def test_history_keeps_original_record(db):
    rec = sample_run(db, "E2")
    stored = storage.get_run(rec["run_id"], db)
    assert stored["internal_reasons"] == rec["internal_reasons"]
    assert stored["vendor_message"] == rec["vendor_message"]


def test_mailto_roundtrip():
    msg = {"to": "vendor@example.com", "subject": "A & B", "body": "Line1\nLine2"}
    link = v.build_mailto(msg)
    assert link.startswith("mailto:vendor@example.com?subject=A%20%26%20B&body=Line1%0ALine2")


def test_sample_pdfs_exist_and_are_pdfs():
    expected = [rel for s in SAMPLES for rel in sample_docs_for(s).values()] + list(NEGATIVE_EXAMPLES)
    assert len(expected) == 20
    for rel in expected:
        assert (SAMPLE_DOCS / rel).read_bytes().startswith(b"%PDF"), rel


# ============================================================ document-content checks
def test_doc1_correct_documents_on_disk_approve_h1_and_a8(db):
    rec = disk_run(db, "H1")
    assert rec["decision"] == "APPROVED"
    stage2 = stage(rec, "2.")
    assert stage2["status"] == "PASS"
    assert check(rec, "Registration proof - document type")["detail"].startswith("Document content checked")
    assert "matches the submitted tax ID" in check(rec, "Tax proof - tax ID")["detail"]
    assert "not an authenticity check" in stage2["detail"]
    assert disk_run(db, "A8")["decision"] == "APPROVED"  # US vendor, EIN document


def test_doc2_registration_in_bank_slot_is_pending(db):
    sub = make_sub(H1, bank_proof=disk_upload("H1_northstar/northstar_registration_proof.pdf"))
    rec = process(db, sub)
    assert rec["decision"] == "PENDING"
    assert "Document type mismatch" in reasons(rec)
    assert "company registration evidence" in reasons(rec)
    assert check(rec, "Bank proof - document type")["status"] == "FLAG"
    assert "bank details proof" in rec["vendor_message"]["body"]


def test_doc3_tax_document_with_different_tax_id_is_pending(db):
    rec = process(db, make_sub(H1, tax_proof=disk_upload("negative_examples/northstar_tax_proof_wrong_tax_id.pdf")))
    assert rec["decision"] == "PENDING"
    assert "Tax ID mismatch" in reasons(rec) and "27ZZZZZ9999Z1Z5" in reasons(rec)
    assert "tax ID on your tax document" in rec["vendor_message"]["body"]


def test_doc4_registration_with_different_company_is_pending(db):
    rec = process(db, make_sub(H1, registration_proof=disk_upload(
        "negative_examples/northstar_registration_proof_wrong_company.pdf")))
    assert rec["decision"] == "PENDING"
    assert "Harborline Traders" in reasons(rec) and "does not match the submitted legal name" in reasons(rec)


def test_doc5_bank_proof_with_inconsistent_details_is_pending(db):
    bad_holder = pdf_upload("b.pdf", bank_pdf(H1["bank_name"], "Someone Else Pvt Ltd", H1["account_last4"]))
    rec = process(db, make_sub(H1, bank_proof=bad_holder))
    assert rec["decision"] == "PENDING" and "account holder in the document" in reasons(rec)

    bad_digits = pdf_upload("b.pdf", bank_pdf(H1["bank_name"], H1["account_holder"], "9999"))
    rec = process(db, make_sub({**H1, "tax_id": "24AAAAA1111A1Z1"}, bank_proof=bad_digits))
    assert rec["decision"] == "PENDING" and "last four digits" in reasons(rec)

    bad_bank = pdf_upload("b.pdf", bank_pdf("Other Bank", H1["account_holder"], H1["account_last4"]))
    rec = process(db, make_sub({**H1, "tax_id": "07AAAAA2222B1Z3"}, bank_proof=bad_bank))
    assert rec["decision"] == "PENDING" and "bank name in the document" in reasons(rec)


def test_doc5b_bank_holder_matching_legal_but_not_form_holder_is_flagged(db):
    fields = {**H1, "account_holder": "Northstar Trading Pvt Ltd"}
    rec = process(db, make_sub(fields, doc_fields=H1))  # bank doc names the legal company
    assert "It does match the legal company name" in reasons(rec)
    assert rec["decision"] == "PENDING"


def test_doc6_scanned_image_only_pdf_needs_manual_verification(db):
    rec = process(db, make_sub(H1, tax_proof=disk_upload("negative_examples/scanned_style_no_text.pdf")))
    assert rec["decision"] == "PENDING"
    assert "Manual verification required" in reasons(rec)
    assert check(rec, "Tax proof - content")["status"] == "FLAG"
    assert "text-based PDF" in rec["vendor_message"]["body"]


def test_doc6b_image_upload_is_never_marked_content_checked(db):
    png = v.Upload("scan.png", 2000, "image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    rec = process(db, make_sub(H1, registration_proof=png))
    assert rec["decision"] == "PENDING"
    assert "contents were not analysed" in reasons(rec)
    assert check(rec, "Registration proof - content")["status"] == "FLAG"


@pytest.mark.parametrize("name,data", [
    ("broken.pdf", b"%PDF-1.4\nthis is not a real pdf body"),
    ("fake.pdf", b"MZ\x90\x00 this is an executable, not a PDF"),
    ("truncated.pdf", document_pdfs(H1, ("bank_proof",))["bank_proof"][:300]),
    ("empty_bytes.pdf", b""),
])
def test_doc7_corrupted_or_invalid_pdf_is_flagged_not_crashed(db, name, data):
    rec = process(db, make_sub(H1, bank_proof=v.Upload(name, max(len(data), 1), "application/pdf", data)))
    assert rec["execution_status"] == "SUCCESS" and rec["decision"] == "PENDING"
    assert "could not be read automatically" in reasons(rec)
    assert check(rec, "Bank proof - content")["status"] == "FLAG"


def test_doc8_missing_document_is_pending_with_specific_request(db):
    rec = run(db, H1, attach=("registration_proof", "tax_proof"))
    assert rec["decision"] == "PENDING"
    assert "Bank proof missing" in reasons(rec) and "bank details proof" in rec["vendor_message"]["body"]
    assert check(rec, "Bank proof - content")["status"] == "SKIP"  # unable to evaluate, not a false pass


def test_doc9_document_discrepancies_with_denylist_still_rejected(db):
    s = SAMPLE_BY_ID["E4"]
    wrong_slot = pdf_upload("x.pdf", registration_pdf(s["fields"]["legal_name"], "India"))
    rec = process(db, make_sub(s["fields"], bank_proof=wrong_slot))
    assert rec["decision"] == "REJECTED"
    assert "synthetic internal denylist" in reasons(rec) and "Document type mismatch" in reasons(rec)
    assert rec["internal_reasons"][0].startswith("Tax identity")  # policy reason leads
    assert "denylist" not in rec["vendor_message"]["body"].lower()


def test_doc10_repeated_valid_identity_is_duplicate_review(db):
    first = disk_run(db, "H1")
    second = disk_run(db, "H1")
    assert first["decision"] == "APPROVED"
    assert second["decision"] == "PENDING"
    assert first["run_id"][:8] in reasons(second) and "duplicate" in reasons(second).lower()


def test_doc11_new_vendors_with_matching_documents_get_dynamic_decisions(db):
    fields = dict(legal_name="Maple & Birch Trading Co.", contact_email="ap@maplebirch.example",
                  country="India", tax_id="19MAPLE5678B1Z4", bank_name="Demo Bank",
                  account_holder="MAPLE BIRCH TRADING CO", account_last4="7788")
    assert run(db, fields)["decision"] == "APPROVED"
    us = dict(legal_name="Pinecrest Freight Inc", contact_email="ops@pinecrest.example",
              country="United States", tax_id="98-7654321", bank_name="Demo Bank",
              account_holder="Pinecrest Freight Inc", account_last4="1357")
    assert run(db, us)["decision"] == "APPROVED"
    other = {**fields, "tax_id": "19MAPLE5678B1Z5"}  # same engine, one document disagrees
    assert run(db, other, doc_fields={**other, "tax_id": "19MAPLE5678B1Z9"})["decision"] == "PENDING"


def test_decision_depends_on_content_not_on_names_or_slots(db):
    renamed = {**H1, "legal_name": "Completely Different Name Ltd", "account_holder": "Completely Different Name Ltd",
               "tax_id": "06ZZZZZ0000Z1Z1"}
    assert run(db, renamed)["decision"] == "APPROVED"          # matching docs, any name
    assert run(db, H1, doc_fields=renamed)["decision"] == "PENDING"  # docs of another company


@pytest.mark.parametrize("sid,expected_reason", [
    ("E1", "Tax proof missing"),
    ("E2", "does not match legal name"),
    ("E3", "United States format"),
    ("E4", "synthetic internal denylist"),
])
def test_edge_case_documents_add_no_unrelated_discrepancies(db, sid, expected_reason):
    rec = disk_run(db, sid)
    assert len(rec["internal_reasons"]) == 1, reasons(rec)
    assert expected_reason in reasons(rec)
    assert rec["decision"] == ("REJECTED" if sid == "E4" else "PENDING")


def test_extracted_text_is_sanitized_and_bounded():
    assert v.clean_extracted("<script>alert(1)</script>\x00Name") == "script alert(1) /script Name"
    assert len(v.clean_extracted("A" * 500)) == 80
    pdf = registration_pdf("Evil <b>Corp</b> " + "X" * 300, "India")
    text, _ = v.extract_pdf_text(pdf)
    name = v.field_value(text, v.NAME_LABELS)
    assert "<" not in name and ">" not in name and len(name) <= 80


def test_classifier_is_content_based_and_explainable():
    pdfs = document_pdfs(H1)
    kinds = {slot: v.classify_document(v.extract_pdf_text(data)[0])[0] for slot, data in pdfs.items()}
    assert kinds == {slot: slot for slot in ALL_SLOTS}
    assert v.classify_document("Quarterly newsletter about office plants and holiday schedules")[0] is None
    assert v.classify_document("")[0] is None


def test_us_tax_hyphen_normalization_in_document_comparison(db):
    fields = {**SAMPLE_BY_ID["A8"]["fields"], "tax_id": "123456789"}
    rec = process(db, make_sub(fields, doc_fields={**fields, "tax_id": "12-3456789"}))
    assert rec["decision"] == "APPROVED"


def test_uploaded_bytes_are_never_stored(db):
    rec = disk_run(db, "H1")
    stored = json.dumps(storage.get_run(rec["run_id"], db))
    assert "%PDF" not in stored and "FICTIONAL SAMPLE DOCUMENT" not in stored
    assert "content" not in rec["attachments"][0]


def test_missing_pdf_library_is_an_honest_error_not_a_decision(db, monkeypatch):
    monkeypatch.setattr(v, "PdfReader", None)
    rec = process(db, make_sub(H1))
    assert rec["execution_status"] == "ERROR" and rec["decision"] is None
    assert "pypdf" in reasons(rec)
    assert storage.counts(db)["total"] == 0


def test_unreadable_or_mismatched_document_never_passes_silently(db):
    for bad in (b"", b"%PDF-1.4 junk", build_pdf("", []), build_pdf("MENU", ["Soup: 5", "Salad: 6 " * 5])):
        rec = process(db, make_sub({**H1, "tax_id": "24AAAAA1111A1Z1"}, tax_proof=pdf_upload("t.pdf", bad, size=max(len(bad), 1))))
        assert rec["decision"] == "PENDING", bad[:20]
