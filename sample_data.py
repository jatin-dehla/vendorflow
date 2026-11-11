"""Fictional demo data: policy fixtures and sample vendor inputs.

Everything here is synthetic. Samples only pre-fill the SAME form fields a user
would type into; they never decide an outcome. Outcomes come from validation.py.
"""

# Synthetic internal denylist: normalized (country, tax_id) pairs.
# A fictional demo policy fixture -- NOT a real sanctions or fraud database.
SYNTHETIC_DENYLIST = {
    ("India", "33BLOCK1234X1Z9"),
}

ALL_SLOTS = ("registration_proof", "tax_proof", "bank_proof")

_DEFAULTS = {
    "legal_name": "Northstar Office Supplies Pvt Ltd",
    "contact_email": "vendor@example.com",
    "country": "India",
    "tax_id": "27ABCDE1234F1Z5",
    "bank_name": "Sample Bank",
    "account_holder": "Northstar Office Supplies Pvt Ltd",
    "account_last4": "4821",
}


def _sample(sample_id, label, attach=ALL_SLOTS, **overrides):
    fields = dict(_DEFAULTS)
    fields.update(overrides)
    return {"id": sample_id, "label": label, "fields": fields, "attach": tuple(attach)}


# TEST_PLAN.md section 3 fixtures (+ a US happy path from A8).
SAMPLES = [
    _sample("H1", "H1 - Happy path: Northstar Office Supplies (India)"),
    _sample(
        "E1", "E1 - Missing tax proof: Bluefield Services (India)",
        attach=("registration_proof", "bank_proof"),
        legal_name="Bluefield Services Pvt Ltd",
        account_holder="Bluefield Services Pvt Ltd",
        tax_id="29PQRST5678K1Z8",
    ),
    _sample(
        "E2", "E2 - Bank-holder name mismatch: Riverbend Components (India)",
        legal_name="Riverbend Components Pvt Ltd",
        account_holder="Riverbend Trading Pvt Ltd",
        tax_id="27LMNOP2345Q1Z6",
    ),
    _sample(
        "E3", "E3 - India-format tax ID for a US vendor: Westbridge Systems",
        legal_name="Westbridge Systems LLC",
        account_holder="Westbridge Systems LLC",
        country="United States",
        tax_id="27ABCDE1234F1Z5",
    ),
    _sample(
        "E4", "E4 - Synthetic denylist match: Sample Restricted Vendor",
        legal_name="Sample Restricted Vendor Pvt Ltd",
        account_holder="Sample Restricted Vendor Pvt Ltd",
        tax_id="33BLOCK1234X1Z9",
    ),
    _sample(
        "A8", "A8 - US happy path: Lakeshore Logistics LLC",
        legal_name="Lakeshore Logistics LLC",
        account_holder="Lakeshore Logistics LLC",
        country="United States",
        tax_id="12-3456789",
    ),
]


# ---------------------------------------------------------------------------
# Fictional sample documents. Each scenario has its own folder in sample_docs/ whose PDFs
# state that scenario's own company, tax ID, account holder and bank, so a correct upload
# matches the form. Files are produced by generate_sample_docs.py from the SAME fields the
# form is filled with; no outcome is stored here. Contents are fictional.
# ---------------------------------------------------------------------------
SAMPLE_FOLDERS = {  # sample id -> (folder, short filename prefix)
    "H1": ("H1_northstar", "northstar"),
    "E1": ("E1_bluefield", "bluefield"),
    "E2": ("E2_riverbend", "riverbend"),
    "E3": ("E3_westbridge", "westbridge"),
    "E4": ("E4_restricted_vendor", "restricted"),
    "A8": ("A8_lakeshore", "lakeshore"),
}
SLOT_FILE_PART = {"registration_proof": "registration_proof", "tax_proof": "tax_proof",
                  "bank_proof": "bank_proof"}


def sample_doc_path(sample_id, slot):
    """Path (relative to sample_docs/) of a scenario's document for an upload slot."""
    folder, prefix = SAMPLE_FOLDERS[sample_id]
    return f"{folder}/{prefix}_{SLOT_FILE_PART[slot]}.pdf"


def sample_docs_for(sample):
    """{slot: relative path} for the documents a scenario is meant to upload."""
    return {slot: sample_doc_path(sample["id"], slot) for slot in sample["attach"]}


# Documents that demonstrate a mismatch (used with the Northstar form; see README).
NEGATIVE_EXAMPLES = {
    "negative_examples/northstar_tax_proof_wrong_tax_id.pdf": "Tax proof showing a different tax ID",
    "negative_examples/northstar_registration_proof_wrong_company.pdf": "Registration proof for a different company",
    "negative_examples/scanned_style_no_text.pdf": "Image-only PDF with no readable text",
}


def _esc(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(title, lines):
    """Minimal valid one-page PDF with real, extractable text (standard library only)."""
    ops = []
    if title:
        ops.append("BT /F2 16 Tf 56 790 Td (%s) Tj ET" % _esc(title))
    y = 750
    for line in lines:
        if line:
            ops.append("BT /F1 11 Tf 56 %d Td (%s) Tj ET" % (y, _esc(line)))
        y -= 20
    if not title and not lines:  # drawing only, no text: behaves like a scanned page
        ops.append("0.8 g 56 400 480 300 re f")
    content = "\n".join(ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


_NOTICE = "FICTIONAL SAMPLE DOCUMENT - NOT A REAL RECORD - FOR THE VENDORFLOW DEMO ONLY"


def registration_pdf(legal_name, country):
    return build_pdf("CERTIFICATE OF INCORPORATION", [
        _NOTICE, "",
        f"Registered Company Name: {legal_name}",
        "Registration Number: DEMO-REG-000123",
        f"Country of Registration: {country}",
        "Date of Incorporation: 01 January 2020",
    ])


def tax_pdf(legal_name, country, tax_id):
    if country == "United States":
        return build_pdf("EIN CONFIRMATION LETTER", [
            _NOTICE, "",
            f"Legal Name: {legal_name}",
            f"Employer Identification Number (EIN): {tax_id}",
            "Country: United States",
        ])
    return build_pdf("TAX REGISTRATION CERTIFICATE (GST)", [
        _NOTICE, "",
        f"Legal Name: {legal_name}",
        f"GSTIN: {tax_id}",
        f"Country: {country}",
    ])


def bank_pdf(bank_name, account_holder, last4):
    return build_pdf("BANK ACCOUNT CONFIRMATION LETTER", [
        _NOTICE, "",
        f"Bank Name: {bank_name}",
        f"Account Holder: {account_holder}",
        f"Account Number: XXXXXXXX{last4}",
    ])


def document_pdfs(fields, slots=ALL_SLOTS):
    """{slot: pdf bytes} stating exactly what the given form fields say."""
    builders = {
        "registration_proof": lambda: registration_pdf(fields["legal_name"], fields["country"]),
        "tax_proof": lambda: tax_pdf(fields["legal_name"], fields["country"], fields["tax_id"]),
        "bank_proof": lambda: bank_pdf(fields["bank_name"], fields["account_holder"], fields["account_last4"]),
    }
    return {slot: builders[slot]() for slot in slots}
