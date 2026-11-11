"""VendorFlow validation rules, six-stage pipeline and decision engine.

Pure Python, no Streamlit or database imports. The UI and the database are
passed in as plain functions (find_prior, save), so every rule is testable alone.

What this module does NOT do: verify tax registration, verify bank ownership,
authenticate documents, or screen against any real sanctions list. Document checks read
the text of machine-readable PDFs only (pypdf, no OCR) and compare it with the form.
"""
from __future__ import annotations

import io
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote

from sample_data import SYNTHETIC_DENYLIST

try:  # PDF text extraction; the only third-party dependency besides Streamlit
    from pypdf import PdfReader
except ImportError:  # reported honestly as an ERROR run if a PDF must be read
    PdfReader = None

# ---------------------------------------------------------------- constants
COUNTRIES = ("India", "United States")
ALLOWED_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg")
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB, enforced here and not only in the UI

DOC_LABELS = {
    "registration_proof": "Registration proof",
    "tax_proof": "Tax proof",
    "bank_proof": "Bank proof",
}
DOC_REQUEST_NAMES = {
    "registration_proof": "company registration document",
    "tax_proof": "tax registration document",
    "bank_proof": "bank details proof",
}

# Format-only patterns (see PROJECT_SPEC.md section 3). Not registry lookups.
INDIA_GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
US_EIN = re.compile(r"^\d{2}-?\d{7}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

NAME_STAGE_1 = "1. Receive submission"
NAME_STAGE_2 = "2. Check documents"
NAME_STAGE_3 = "3. Check tax information"
NAME_STAGE_4 = "4. Check banking consistency"
NAME_STAGE_5 = "5. Check internal policy and duplicates"
NAME_STAGE_6 = "6. Decide and prepare next action"


# ---------------------------------------------------------------- data model
@dataclass(frozen=True)
class Upload:
    """An uploaded file: metadata plus its bytes for in-memory reading. Bytes are never stored."""
    filename: str
    size_bytes: int
    declared_type: str = ""
    content: bytes = field(default=b"", repr=False, compare=False)  # read in memory, never stored


@dataclass
class Submission:
    """Structured input. Validation never reads UI widgets, only this object."""
    legal_name: str = ""
    contact_email: str = ""
    country: str = ""
    tax_id: str = ""
    bank_name: str = ""
    account_holder: str = ""
    account_last4: str = ""
    documents: dict = field(default_factory=dict)  # slot -> Upload | None


@dataclass
class Issue:
    code: str
    kind: str          # missing | invalid | review | duplicate | policy
    internal: str      # explanation for the analyst
    action: str        # next step for the analyst
    vendor_text: str   # wording used in the vendor-facing draft


@dataclass
class StageResult:
    name: str
    status: str = "PASS"            # PASS | FLAG | SKIP | ERROR
    checks: list = field(default_factory=list)
    detail: str = ""
    issues: list = field(default_factory=list)

    def add(self, label, status, detail, issue=None):
        self.checks.append({"label": label, "status": status, "detail": detail})
        if issue is not None:
            self.issues.append(issue)

    def finish(self):
        passed = sum(c["status"] == "PASS" for c in self.checks)
        flagged = sum(c["status"] == "FLAG" for c in self.checks)
        skipped = sum(c["status"] == "SKIP" for c in self.checks)
        if flagged:
            self.status = "FLAG"
        elif self.checks and skipped == len(self.checks):
            self.status = "SKIP"
        else:
            self.status = "PASS"
        self.detail = f"{passed} checks passed; {flagged} require attention"
        if skipped:
            self.detail += f"; {skipped} could not be evaluated"
        return self

    def to_dict(self):
        return {"name": self.name, "status": self.status,
                "checks": self.checks, "detail": self.detail}


# ---------------------------------------------------------------- helpers
def clean(value) -> str:
    return (value or "").strip()


def normalize_name(name: str) -> str:
    """Lowercase, punctuation -> space, collapse spaces. No fuzzy matching."""
    lowered = clean(name).lower()
    no_punct = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", no_punct).strip()


def canonical_tax_id(country: str, tax_id: str) -> str:
    """Trim + uppercase; for US also drop the optional hyphen."""
    value = clean(tax_id).upper()
    if country == "United States":
        value = value.replace("-", "")
    return value


def mask_tax_id(tax_id: str) -> str:
    value = clean(tax_id)
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def tax_format_ok(country: str, tax_id: str) -> bool:
    value = clean(tax_id).upper()
    if country == "India":
        return bool(INDIA_GSTIN.match(value))
    if country == "United States":
        return bool(US_EIN.match(value))
    return False


def _short(run_id: str) -> str:
    return run_id[:8]


# ---------------------------------------------------------------- stage 1
def check_submission(sub, ctx):
    if not isinstance(sub, Submission):
        raise TypeError("Submission payload was not received.")
    s = StageResult(NAME_STAGE_1)
    s.add("Submission payload", "PASS", "Form data received.")

    if not clean(sub.legal_name):
        s.add("Legal company name", "FLAG", "Missing.", Issue(
            "legal_name_missing", "missing",
            "Legal company name is missing.",
            "Ask the vendor for the legal company name.",
            "Please provide your registered legal company name."))
    else:
        s.add("Legal company name", "PASS", "Provided.")

    email = clean(sub.contact_email)
    if not email:
        s.add("Contact email", "FLAG", "Missing.", Issue(
            "contact_email_missing", "missing",
            "Contact email is missing.",
            "Obtain a contact email for the vendor.",
            "Please provide a contact email address."))
    elif not EMAIL.match(email):
        s.add("Contact email", "FLAG", "Not a valid email format.", Issue(
            "contact_email_invalid", "invalid",
            f"Contact email '{email}' is not a valid email format.",
            "Obtain a corrected contact email.",
            "Please provide a valid contact email address (for example name@company.com)."))
    else:
        s.add("Contact email", "PASS", "Valid email syntax.")

    if sub.country not in COUNTRIES:
        s.add("Country", "FLAG", "Missing or unsupported.", Issue(
            "country_invalid", "invalid",
            "Country is missing or not one of the supported demo countries.",
            "Confirm the vendor's country (India or United States).",
            "Please confirm your country of registration."))
    else:
        s.add("Country", "PASS", f"{sub.country}.")

    if not clean(sub.tax_id):
        s.add("Tax ID", "FLAG", "Missing.", Issue(
            "tax_id_missing", "missing",
            "Tax ID is missing.",
            "Ask the vendor for the tax registration number.",
            "Please provide your tax registration number."))
    else:
        s.add("Tax ID", "PASS", "Provided.")
    return s.finish()


# ---------------------------------------------------------------- stage 2
# Document-content checks. Deterministic and rule-based: text is read from PDFs with
# pypdf, the document type is recognised from fixed keyword evidence, and a few
# "Label: value" fields are compared with the form using the same normalization as
# the rest of the engine. A match means the document is internally consistent with the
# submission. It does NOT authenticate the document, and it only covers clearly
# structured, text-based PDFs (no OCR). Images and unreadable files need manual review.
MAX_PDF_PAGES = 5
MAX_TEXT_CHARS = 20000
MIN_TEXT_CHARS = 40  # fewer letters/digits than this is treated as "no readable text"

DOC_TYPE_NAMES = {
    "registration_proof": "company registration evidence",
    "tax_proof": "tax registration evidence",
    "bank_proof": "banking evidence",
}
# slot -> (title phrases worth 3 points if any appears, field labels worth 1 point each)
DOC_SIGNALS = {
    "registration_proof": (
        ("certificate of incorporation", "certificate of registration", "company registration"),
        ("registered company name", "date of incorporation", "country of registration"),
    ),
    "tax_proof": (
        ("tax registration certificate", "gst registration", "ein confirmation", "tax identification"),
        ("gstin", "employer identification number", "tax registration number", "tax id"),
    ),
    "bank_proof": (
        ("bank account confirmation", "bank confirmation", "bank account details", "bank details", "bank letter"),
        ("account holder", "bank name", "account number"),
    ),
}
NAME_LABELS = ("registered company name", "legal name", "company name", "name of company",
               "entity name", "registered name")
TAX_ID_LABELS = ("gstin", "tax registration number", "employer identification number", "ein",
                 "tax identification number", "tax id")
HOLDER_LABELS = ("account holder name", "account holder", "account name", "beneficiary name")
BANK_LABELS = ("bank name", "bank")
ACCOUNT_LABELS = ("account number", "account no", "account ending")


def clean_extracted(value) -> str:
    """Make text taken from a PDF safe to show: no control characters or angle brackets."""
    value = re.sub(r"[<>\x00-\x1f\x7f]", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()[:80]


def extract_pdf_text(data):
    """Return (text, problem). problem is '' when readable text was extracted."""
    if PdfReader is None:
        raise RuntimeError("the 'pypdf' package is not installed (pip install -r requirements.txt)")
    if not data:
        return "", "no file content was received"
    if data[:1024].find(b"%PDF-") == -1:
        return "", "the file is not a valid PDF"
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            return "", "the PDF is password-protected"
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])
    except Exception as exc:  # corrupted or unsupported PDF: reported, never fatal
        return "", f"the PDF could not be read ({type(exc).__name__})"
    text = text[:MAX_TEXT_CHARS]
    if len(re.sub(r"\s+", "", text)) < MIN_TEXT_CHARS:
        return text, "no readable text was found (scanned or image-only PDF?)"
    return text, ""


def classify_document(text):
    """Return (slot or None, scores). A type needs >= 3 points and a clear lead."""
    flat = re.sub(r"\s+", " ", text.lower())
    scores = {}
    for slot, (titles, labels) in DOC_SIGNALS.items():
        scores[slot] = (3 if any(t in flat for t in titles) else 0) + sum(1 for lb in labels if lb in flat)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if ranked[0][1] >= 3 and ranked[0][1] > ranked[1][1]:
        return ranked[0][0], scores
    return None, scores


def field_value(text, labels):
    """First 'Label: value' line found, trying labels in order. '' if none."""
    for label in labels:
        pattern = (r"(?im)^[ \t]*" + re.escape(label) +
                   r"(?:[ \t]*\([^)\n]*\))?[ \t]*[:\-][ \t]*(.+?)[ \t]*$")
        match = re.search(pattern, text)
        if match and clean_extracted(match.group(1)):
            return clean_extracted(match.group(1))
    return ""


def _flag(s, check, detail, code, kind, internal, action, vendor):
    s.add(check, "FLAG", detail, Issue(code, kind, internal, action, vendor))


def _check_registration(s, sub, text, label, request):
    check = f"{label} - company name"
    doc_name, legal = field_value(text, NAME_LABELS), clean(sub.legal_name)
    if not doc_name:
        _flag(s, check, "No company name found in the document (essential information missing).",
              "registration_proof_name_missing", "invalid",
              f"{label}: no company name could be found in the document (essential information missing).",
              "Ask the vendor for a registration document that states the company name.",
              "We could not find the company name in your registration document. Please upload a clear, complete copy.")
    elif not legal:
        s.add(check, "SKIP", "Cannot evaluate: legal company name is missing in the form.")
    elif normalize_name(doc_name) == normalize_name(legal):
        s.add(check, "PASS", f"Extracted name '{doc_name}' matches the submitted legal name.")
    else:
        _flag(s, check, f"Extracted name '{doc_name}' differs from submitted legal name '{legal}'.",
              "registration_proof_name_mismatch", "review",
              f"{label}: company name in the document ('{doc_name}') does not match the submitted legal "
              f"name ('{legal}'). Manual verification required.",
              "Manual verification: confirm the correct legal name and obtain a matching registration document.",
              "The company name on your registration document differs from the legal company name in your "
              "submission. Please confirm the correct name, or upload the matching document.")


def _check_tax(s, sub, text, label, request):
    check = f"{label} - tax ID"
    doc_id = canonical_tax_id(sub.country, field_value(text, TAX_ID_LABELS))
    form_id = canonical_tax_id(sub.country, sub.tax_id)
    if not doc_id:
        _flag(s, check, "No tax ID found in the document (essential information missing).",
              "tax_proof_id_missing", "invalid",
              f"{label}: no tax ID could be found in the document (essential information missing).",
              "Ask the vendor for a tax document that states the tax ID.",
              "We could not find the tax ID in your tax document. Please upload a clear, complete copy.")
    elif not form_id:
        s.add(check, "SKIP", "Cannot evaluate: tax ID is missing in the form.")
    elif doc_id == form_id:
        s.add(check, "PASS", f"Extracted tax ID '{doc_id}' matches the submitted tax ID.")
    else:
        _flag(s, check, f"Tax ID mismatch: document shows '{doc_id}', form has '{form_id}'.",
              "tax_proof_id_mismatch", "invalid",
              f"Tax ID mismatch: the tax proof shows '{doc_id}' but the submitted tax ID is '{form_id}'.",
              "Ask the vendor to confirm the correct tax ID and upload the matching tax document.",
              "The tax ID on your tax document does not match the tax ID in your submission. Please confirm "
              "the correct tax ID and upload the matching document.")

    check = f"{label} - company name"
    doc_name, legal = field_value(text, NAME_LABELS), clean(sub.legal_name)
    if not doc_name:
        s.add(check, "SKIP", "No company name stated in the document; not compared.")
    elif not legal:
        s.add(check, "SKIP", "Cannot evaluate: legal company name is missing in the form.")
    elif normalize_name(doc_name) == normalize_name(legal):
        s.add(check, "PASS", f"Extracted name '{doc_name}' matches the submitted legal name.")
    else:
        _flag(s, check, f"Extracted name '{doc_name}' differs from submitted legal name '{legal}'.",
              "tax_proof_name_mismatch", "review",
              f"{label}: company name in the document ('{doc_name}') does not match the submitted legal "
              f"name ('{legal}'). Manual verification required.",
              "Manual verification: confirm the legal name and obtain a matching tax document.",
              "The company name on your tax document differs from the legal company name in your "
              "submission. Please confirm the correct name, or upload the matching document.")


def _check_bank(s, sub, text, label, request):
    check = f"{label} - account holder"
    doc_holder, holder = field_value(text, HOLDER_LABELS), clean(sub.account_holder)
    legal = clean(sub.legal_name)
    if not doc_holder:
        _flag(s, check, "No account holder found in the document (essential information missing).",
              "bank_proof_holder_missing", "invalid",
              f"{label}: no account holder could be found in the document (essential information missing).",
              "Ask the vendor for a bank document that shows the account holder name.",
              "We could not find the account holder name in your bank document. Please upload a clear, complete copy.")
    elif not holder:
        s.add(check, "SKIP", "Cannot evaluate: account holder is missing in the form.")
    elif normalize_name(doc_holder) == normalize_name(holder):
        note = ""
        if legal and normalize_name(doc_holder) != normalize_name(legal):
            note = " (differs from the legal company name; see the banking check)"
        s.add(check, "PASS", f"Extracted holder '{doc_holder}' matches the submitted account holder{note}.")
    else:
        also = " It does match the legal company name." if legal and normalize_name(doc_holder) == normalize_name(legal) else ""
        _flag(s, check, f"Extracted holder '{doc_holder}' differs from submitted holder '{holder}'.{also}",
              "bank_proof_holder_mismatch", "review",
              f"{label}: account holder in the document ('{doc_holder}') does not match the submitted "
              f"account holder ('{holder}').{also} Manual verification required.",
              "Manual verification: confirm the account holder and obtain a matching bank document.",
              "The account holder name on your bank document differs from the account holder in your "
              "submission. Please confirm the correct name, or upload the matching document.")

    check = f"{label} - bank name"
    doc_bank, bank = field_value(text, BANK_LABELS), clean(sub.bank_name)
    if not doc_bank:
        s.add(check, "SKIP", "No bank name stated in the document; not compared.")
    elif not bank:
        s.add(check, "SKIP", "Cannot evaluate: bank name is missing in the form.")
    elif normalize_name(doc_bank) == normalize_name(bank):
        s.add(check, "PASS", f"Extracted bank '{doc_bank}' matches the submitted bank name.")
    else:
        _flag(s, check, f"Extracted bank '{doc_bank}' differs from submitted bank '{bank}'.",
              "bank_proof_bank_mismatch", "invalid",
              f"{label}: bank name in the document ('{doc_bank}') does not match the submitted bank name ('{bank}').",
              "Ask the vendor to confirm the bank name and upload the matching bank document.",
              "The bank name on your bank document differs from the bank name in your submission. "
              "Please confirm the correct bank, or upload the matching document.")

    check = f"{label} - account last four digits"
    tail = re.search(r"(\d{4})\s*$", field_value(text, ACCOUNT_LABELS))
    last4 = clean(sub.account_last4)
    if not tail:
        s.add(check, "SKIP", "No account number stated in the document; not compared.")
    elif not re.fullmatch(r"\d{4}", last4):
        s.add(check, "SKIP", "Cannot evaluate: account last four digits are missing or invalid in the form.")
    elif tail.group(1) == last4:
        s.add(check, "PASS", "Last four digits in the document match the submitted digits.")
    else:
        _flag(s, check, "Last four digits in the document differ from the submitted digits.",
              "bank_proof_last4_mismatch", "invalid",
              f"{label}: the account digits in the document do not match the submitted last four digits.",
              "Ask the vendor to confirm the account digits and upload the matching bank document.",
              "The account digits on your bank document do not match the digits in your submission. "
              "Please confirm the correct digits, or upload the matching document.")


CONTENT_CHECKS = {"registration_proof": _check_registration, "tax_proof": _check_tax, "bank_proof": _check_bank}


def check_document_content(s, sub, slot, label, up):
    """Read one accepted upload and compare what it says with the form."""
    request = DOC_REQUEST_NAMES[slot]
    check = f"{label} - content"
    if os.path.splitext(clean(up.filename).lower())[1] != ".pdf":
        _flag(s, check, "Image file: contents are not analysed. Manual verification required.",
              f"{slot}_image", "review",
              f"{label}: '{up.filename}' is an image, so its contents were not analysed. "
              "Manual verification required.",
              f"Manual verification: review the {request} by hand, or ask the vendor for a text-based PDF.",
              f"Your {request} was uploaded as an image, which cannot be read automatically. Please upload "
              "a text-based PDF copy, or our team will verify it manually.")
        return

    text, problem = extract_pdf_text(up.content)
    if problem:
        _flag(s, check, f"Unreadable: {problem}. Manual verification required.",
              f"{slot}_unreadable", "invalid",
              f"{label}: '{up.filename}' could not be read automatically ({problem}). "
              "Manual verification required.",
              f"Ask the vendor for a clear, text-based PDF of the {request}, or verify it manually.",
              f"We could not read the text in your {request}. Please upload a clear, text-based PDF "
              "(scanned images and damaged files cannot be read automatically).")
        return

    found, _scores = classify_document(text)
    if found != slot:
        if found is None:
            detail = f"Content not recognised as {DOC_TYPE_NAMES[slot]}. Manual verification required."
            internal = (f"{label}: '{up.filename}' could not be recognised as {DOC_TYPE_NAMES[slot]} "
                        "from its content. Manual verification required.")
        else:
            detail = f"Document type mismatch: content reads as {DOC_TYPE_NAMES[found]}."
            internal = (f"Document type mismatch: the file uploaded as {label} ('{up.filename}') reads as "
                        f"{DOC_TYPE_NAMES[found]}, not {DOC_TYPE_NAMES[slot]}.")
        _flag(s, f"{label} - document type", detail, f"{slot}_type_mismatch", "invalid", internal,
              f"Ask the vendor to upload the correct {request}.",
              f"The file uploaded as your {request} appears to be a different kind of document. "
              f"Please upload your {request}.")
        return

    s.add(f"{label} - document type", "PASS", f"Document content checked: recognised as {DOC_TYPE_NAMES[slot]}.")
    CONTENT_CHECKS[slot](s, sub, text, label, request)


def check_documents(sub, ctx):
    s = StageResult(NAME_STAGE_2)
    types = "PDF, PNG or JPG, max 5 MB"
    for slot, label in DOC_LABELS.items():
        up = (sub.documents or {}).get(slot)
        request = DOC_REQUEST_NAMES[slot]
        if up is None:
            s.add(label, "FLAG", "Missing - no file attached.", Issue(
                f"{slot}_missing", "missing",
                f"{label} missing: no file was attached.",
                f"Request the {request} from the vendor.",
                f"Please upload your {request} ({types})."))
            s.add(f"{label} - content", "SKIP", "Not evaluated: no file attached.")
            continue
        ext = os.path.splitext(clean(up.filename).lower())[1]
        if ext not in ALLOWED_EXTENSIONS:
            s.add(label, "FLAG", f"Unsupported file type '{ext or 'none'}'.", Issue(
                f"{slot}_type", "invalid",
                f"{label}: file '{up.filename}' has an unsupported type.",
                f"Ask the vendor to resend the {request} as PDF, PNG or JPG.",
                f"Please re-upload your {request} as a PDF, PNG or JPG file."))
        elif up.size_bytes > MAX_FILE_BYTES:
            s.add(label, "FLAG", "File larger than 5 MB.", Issue(
                f"{slot}_size", "invalid",
                f"{label}: file '{up.filename}' is larger than 5 MB.",
                f"Ask the vendor to resend a smaller {request}.",
                f"Please re-upload your {request} as a file under 5 MB."))
        elif up.size_bytes <= 0:
            s.add(label, "FLAG", "File is empty.", Issue(
                f"{slot}_empty", "invalid",
                f"{label}: file '{up.filename}' is empty.",
                f"Ask the vendor to resend the {request}.",
                f"The {request} you uploaded appears to be empty. Please upload it again."))
        else:
            kb = max(1, up.size_bytes // 1024)
            s.add(label, "PASS", f"{up.filename} ({kb} KB) - type and size accepted.")
            check_document_content(s, sub, slot, label, up)
            continue
        s.add(f"{label} - content", "SKIP", "Not evaluated: the file was not accepted.")
    s.finish()
    s.detail += (". Content is read from text-based PDFs only; images and unreadable files need manual "
                 "verification. This is not an authenticity check.")
    return s


# ---------------------------------------------------------------- stage 3
def check_tax(sub, ctx):
    s = StageResult(NAME_STAGE_3)
    country = sub.country
    if country not in COUNTRIES or not clean(sub.tax_id):
        s.add("Tax ID format", "SKIP",
              "Cannot evaluate: country or tax ID is missing (see stage 1).")
        return s.finish()
    canon = canonical_tax_id(country, sub.tax_id)
    if country == "India":
        rule = "15-character GSTIN layout (2 digits, 5 letters, 4 digits, 1 letter, 1 letter/digit, 'Z', 1 letter/digit)"
        vendor_rule = ("Please provide a 15-character GSTIN: 2 digits, 5 letters, 4 digits, "
                       "1 letter, 1 letter or digit, the letter Z, then 1 letter or digit.")
    else:
        rule = "EIN layout: 9 digits, optionally written XX-XXXXXXX"
        vendor_rule = "Please provide a 9-digit EIN, written as XX-XXXXXXX (for example 12-3456789)."
    if tax_format_ok(country, sub.tax_id):
        ctx["tax_valid"] = True
        s.add("Tax ID format", "PASS",
              f"'{canon}' matches the {country} {rule}. Format check only - not a registry lookup.")
    else:
        s.add("Tax ID format", "FLAG",
              f"'{canon}' does not match the {country} {rule}. Format check only.",
              Issue("tax_format", "invalid",
                    f"Tax ID '{canon}' is not in a valid {country} format ({rule}).",
                    "Ask the vendor to confirm and resend the tax ID for the selected country.",
                    vendor_rule))
    return s.finish()


# ---------------------------------------------------------------- stage 4
def check_banking(sub, ctx):
    s = StageResult(NAME_STAGE_4)
    holder = clean(sub.account_holder)
    legal = clean(sub.legal_name)

    if not clean(sub.bank_name):
        s.add("Bank name", "FLAG", "Missing.", Issue(
            "bank_name_missing", "missing", "Bank name is missing.",
            "Ask the vendor for the bank name.", "Please provide the name of your bank."))
    else:
        s.add("Bank name", "PASS", "Provided.")

    last4 = clean(sub.account_last4)
    if not last4:
        s.add("Account last four digits", "FLAG", "Missing.", Issue(
            "account_last4_missing", "missing", "Account last four digits are missing.",
            "Ask the vendor for the last four digits of the account.",
            "Please provide the last four digits of your bank account number."))
    elif not re.fullmatch(r"\d{4}", last4):
        s.add("Account last four digits", "FLAG", "Must be exactly four digits.", Issue(
            "account_last4_invalid", "invalid", "Account last four digits are not exactly four digits.",
            "Ask the vendor to resend the last four digits.",
            "Please provide exactly four digits (the last four of your account number)."))
    else:
        s.add("Account last four digits", "PASS", "Four digits provided.")

    if not holder:
        s.add("Account holder", "FLAG", "Missing.", Issue(
            "account_holder_missing", "missing", "Account holder name is missing.",
            "Ask the vendor for the account holder name.",
            "Please provide the account holder name as it appears at your bank."))
    else:
        s.add("Account holder", "PASS", "Provided.")

    if not holder or not legal:
        s.add("Holder vs legal name", "SKIP",
              "Cannot evaluate: legal company name or account holder is missing.")
    elif normalize_name(holder) == normalize_name(legal):
        s.add("Holder vs legal name", "PASS",
              "Names match after normalizing case, punctuation and spacing. "
              "Name comparison only - does not verify account ownership.")
    else:
        s.add("Holder vs legal name", "FLAG",
              f"'{holder}' differs from '{legal}'.",
              Issue("holder_mismatch", "review",
                    f"Account holder '{holder}' does not match legal name '{legal}' after "
                    "normalizing case, punctuation and spacing. Manual review needed; "
                    "this alone is not evidence of wrongdoing.",
                    "Manual review: confirm account ownership with the vendor, e.g. request a "
                    "bank document showing the account holder name.",
                    "The account holder name on the bank details differs from the legal company "
                    "name provided. Please confirm the account holder name, or send a bank document "
                    "that shows it, so our team can complete its review."))
    return s.finish()


# ---------------------------------------------------------------- stage 5
def check_policy(sub, ctx):
    s = StageResult(NAME_STAGE_5)
    country = sub.country
    if country not in COUNTRIES or not clean(sub.tax_id):
        s.add("Synthetic denylist", "SKIP", "Cannot evaluate: country or tax ID is missing.")
        s.add("Duplicate identity", "SKIP", "Cannot evaluate: country or tax ID is missing.")
        return s.finish()

    canon = canonical_tax_id(country, sub.tax_id)
    if (country, canon) in SYNTHETIC_DENYLIST:
        s.add("Synthetic denylist", "FLAG",
              "Matches the synthetic internal denylist (demo fixture, not a real screening service).",
              Issue("denylist", "policy",
                    f"Tax identity ({country}, {mask_tax_id(canon)}) matches the synthetic internal "
                    "denylist. Rejected under internal demo policy.",
                    "Do not proceed with onboarding. Policy rejection is recorded.",
                    ""))
    else:
        s.add("Synthetic denylist", "PASS", "No match in the synthetic internal denylist.")

    if not ctx.get("tax_valid"):
        s.add("Duplicate identity", "SKIP",
              "Cannot evaluate: tax ID is missing or not in a valid format.")
    else:
        prior = ctx["find_prior"](country, canon)
        if prior:
            ref = _short(prior["run_id"])
            s.add("Duplicate identity", "FLAG",
                  f"Same tax identity found in earlier run {ref} ({prior['decision']}).",
                  Issue("duplicate", "duplicate",
                        f"Tax identity already appears in earlier run {ref} "
                        f"(decision: {prior['decision']}). Manual duplicate review required.",
                        f"Manual review: compare with earlier run {ref} before proceeding.",
                        "Our records suggest this tax registration may already be linked to an "
                        "earlier submission. Our team will review this manually; no action is "
                        "needed from you unless we contact you."))
        else:
            s.add("Duplicate identity", "PASS",
                  "No earlier non-rejected submission with this tax identity.")
    return s.finish()


STAGES = [
    (NAME_STAGE_1, check_submission),
    (NAME_STAGE_2, check_documents),
    (NAME_STAGE_3, check_tax),
    (NAME_STAGE_4, check_banking),
    (NAME_STAGE_5, check_policy),
]


# ---------------------------------------------------------------- decision
def decide(issues) -> str:
    """REJECTED beats PENDING beats APPROVED."""
    if any(i.kind == "policy" for i in issues):
        return "REJECTED"
    if issues:
        return "PENDING"
    return "APPROVED"


def build_reasons_and_actions(decision, issues):
    ordered = sorted(issues, key=lambda i: 0 if i.kind == "policy" else 1)
    if decision == "APPROVED":
        return (["Passed configured automated screening checks. This is not identity, "
                 "tax-registration or bank verification."],
                ["No follow-up required."])
    reasons = [i.internal for i in ordered]
    actions = []
    for i in ordered:
        if i.action not in actions:
            actions.append(i.action)
    return reasons, actions


def build_vendor_message(decision, vendor, issues):
    """Generated draft only. Nothing is ever sent."""
    to = clean(vendor.get("contact_email"))
    to = to if EMAIL.match(to) else ""
    name = clean(vendor.get("legal_name")) or "Vendor"
    sign = "Kind regards,\nVendor Onboarding Team"

    if decision == "APPROVED":
        return {"to": "", "subject": "", "body": "", "state": "NOT_REQUIRED"}

    if decision == "REJECTED":
        body = (f"Dear {name} team,\n\n"
                "Thank you for your interest in working with us and for submitting your details.\n\n"
                "After review, we are unable to proceed with your onboarding at this time. "
                "This outcome is based on our internal onboarding policy, and we are not able to "
                "share further details.\n\n"
                "We appreciate your time and wish you every success.\n\n" + sign)
        return {"to": to, "subject": "Update on your vendor onboarding submission",
                "body": body, "state": "DRAFT"}

    fixes = [i.vendor_text for i in issues if i.kind in ("missing", "invalid")]
    reviews = [i.vendor_text for i in issues if i.kind in ("review", "duplicate")]
    parts = [f"Dear {name} team,\n",
             "Thank you for submitting your vendor details. We have reviewed them and need a "
             "little more information before we can continue.\n"]
    if fixes:
        parts.append("Please provide or correct the following:")
        parts += [f"  {n}. {t}" for n, t in enumerate(fixes, 1)]
        parts.append("")
    if reviews:
        parts.append("Our team also needs to complete a manual review:")
        parts += [f"  - {t}" for t in reviews]
        parts.append("")
    parts.append("You can reply to this email with the requested items.\n")
    parts.append(sign)
    return {"to": to, "subject": "Action needed: your vendor onboarding submission",
            "body": "\n".join(parts), "state": "DRAFT"}


def build_mailto(message) -> str:
    """mailto: handoff link for a DRAFT message."""
    return ("mailto:" + quote(message.get("to", ""), safe="@") +
            "?subject=" + quote(message.get("subject", ""), safe="") +
            "&body=" + quote(message.get("body", ""), safe=""))


# ---------------------------------------------------------------- pipeline
def _vendor_dict(sub):
    country = sub.country
    canon = canonical_tax_id(country, sub.tax_id)
    return {
        "legal_name": clean(sub.legal_name),
        "contact_email": clean(sub.contact_email),
        "country": country,
        "masked_tax_id": mask_tax_id(canon),
        "canonical_tax_id": canon,  # used for duplicate matching only; the UI shows the masked value
        "bank_name": clean(sub.bank_name),
        "account_holder": clean(sub.account_holder),
        "account_last4": clean(sub.account_last4),
    }


def _attachments(sub):
    out = []
    for slot, label in DOC_LABELS.items():
        up = (sub.documents or {}).get(slot)
        if up is not None:
            out.append({"document_type": label, "filename": up.filename,
                        "size_bytes": up.size_bytes, "declared_type": up.declared_type})
    return out


def _error_record(run_id, ts, sub, stages, message):
    return {
        "run_id": run_id, "submitted_at_utc": ts, "vendor": _vendor_dict(sub),
        "decision": None, "execution_status": "ERROR",
        "stages": stages,
        "internal_reasons": [message],
        "next_actions": ["Retry the run. No business decision was completed or recorded."],
        "vendor_message": {"to": "", "subject": "", "body": "", "state": "NOT_REQUIRED"},
        "attachments": _attachments(sub),
    }


def process_submission(sub, find_prior, save=None, on_stage=None):
    """Run the six stages in order and return the structured run record.

    find_prior(country, canonical_tax_id) -> dict with run_id/decision, or None
    save(record) -> persists the record; raises on failure
    on_stage(stage_dict) -> called right after each stage really finishes
    """
    run_id = uuid.uuid4().hex
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ctx = {"find_prior": find_prior, "tax_valid": False}
    stage_dicts, issues = [], []

    def emit(stage):
        stage_dicts.append(stage.to_dict())
        if on_stage:
            on_stage(stage.to_dict())

    for name, fn in STAGES:
        try:
            result = fn(sub, ctx)
        except Exception as exc:  # technical failure, never a business decision
            err = StageResult(name, status="ERROR",
                              detail=f"Technical error: {type(exc).__name__}: {exc}")
            emit(err)
            return _error_record(run_id, ts, sub, stage_dicts,
                                 f"Technical error in '{name}': {type(exc).__name__}: {exc}")
        issues.extend(result.issues)
        emit(result)

    # Stage 6: decide, prepare follow-up, persist
    decision = decide(issues)
    reasons, actions = build_reasons_and_actions(decision, issues)
    vendor = _vendor_dict(sub)
    message = build_vendor_message(decision, vendor, issues)

    s6 = StageResult(NAME_STAGE_6)
    s6.add("Decision", "PASS", f"{decision} (REJECTED > PENDING > APPROVED precedence).")
    if message["state"] == "DRAFT":
        s6.add("Vendor follow-up", "PASS", "Draft generated - not sent.")
    else:
        s6.add("Vendor follow-up", "PASS", "No follow-up required.")
    s6.add("Run history", "PASS", "Result written to run history.")
    s6.finish()
    s6.detail = f"Decision: {decision}"

    record = {
        "run_id": run_id, "submitted_at_utc": ts, "vendor": vendor,
        "decision": decision, "execution_status": "SUCCESS",
        "stages": stage_dicts + [s6.to_dict()],
        "internal_reasons": reasons, "next_actions": actions,
        "vendor_message": message, "attachments": _attachments(sub),
    }

    if save is not None:
        try:
            save(record)
        except Exception as exc:
            s6_err = StageResult(NAME_STAGE_6, status="ERROR")
            s6_err.add("Run history", "ERROR", f"Could not save run: {type(exc).__name__}: {exc}")
            s6_err.detail = "Result could not be saved, so no decision is reported as completed."
            emit(s6_err)
            return _error_record(run_id, ts, sub, stage_dicts,
                                 f"The run could not be saved ({type(exc).__name__}: {exc}). "
                                 "No decision was recorded.")
    emit(s6)
    return record
