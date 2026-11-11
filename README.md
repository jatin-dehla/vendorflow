# VendorFlow — Automated Vendor Onboarding & Review

Zamp AI Solutions Analyst case study, **PS-2: Vendor onboarding — from submission to approval**.
A small Streamlit app that takes a vendor submission with three supporting PDFs, runs six real checks
(including reading the text of the PDFs), returns **Approved / Pending / Rejected** with reasons,
drafts a vendor follow-up, and keeps run history.

> Independent candidate case-study application. Not an official Zamp product. Use **fictional data only**.

## Run locally (Windows PowerShell)

```powershell
cd vendorflow
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -r requirements.txt   # streamlit + pypdf
streamlit run app.py
```

Run the tests (pytest is not an app dependency):

```powershell
pip install pytest
python -m pytest -q
```

## Files

```
app.py                     Streamlit UI: Dashboard + New Vendor Review, dialogs, footer
validation.py              Rules, six-stage pipeline, document-content checks, decision, email drafts
storage.py                 SQLite: save, history, duplicate lookup, reset (unchanged schema)
sample_data.py             Fictional samples, synthetic denylist, PDF builder + document templates
generate_sample_docs.py    Regenerates sample_docs/ (standard library only): python generate_sample_docs.py
tests/test_validation.py   Automated tests
sample_docs/               Fictional PDFs, one folder per scenario + negative_examples/
.streamlit/config.toml     Theme + upload limit
pytest.ini                 Lets pytest import the modules from the project root
```

`sample_docs/` layout (every file is fictional and states its own company, tax ID and account details):

```
H1_northstar/            registration_proof, tax_proof, bank_proof   (all match the H1 form)
E1_bluefield/            registration_proof, bank_proof              (tax proof intentionally omitted)
E2_riverbend/            all three (bank document names the form's account holder)
E3_westbridge/           all three (tax document carries the same India-format ID as the form)
E4_restricted_vendor/    all three
A8_lakeshore/            all three (US vendor, EIN)
negative_examples/       tax proof with a different tax ID, registration for a different company,
                         an image-only PDF with no readable text
```

## How it decides

Stages run in order; each is drawn in the live trace right after its function returns (no artificial delays):

1. **Receive submission** — legal name, contact email syntax, country, tax ID present.
2. **Check documents** — for each of registration, tax and bank proof:
   *file checks* (present, `.pdf/.png/.jpg/.jpeg`, ≤ 5 MB enforced in code, not empty), then
   *content checks* (below).
3. **Validate tax information** — India: 15-character GSTIN pattern. US: EIN `XX-XXXXXXX` or 9 digits.
4. **Check banking consistency** — bank name, last four digits (exactly 4), account holder vs legal name after
   lowercase / punctuation → space / collapsed spaces (no fuzzy matching, no stripping of "Pvt Ltd"/"LLC").
5. **Check policy and duplicates** — normalized `(country, tax_id)` against the synthetic denylist and against
   stored **non-rejected** runs (Approved or Pending).
6. **Make and record decision** — decide, draft follow-up, save.

Precedence: **REJECTED** (denylist) › **PENDING** (any other issue) › **APPROVED** (every check passed).
All independent issues are collected. If an input is missing, dependent checks show `SKIP` ("cannot evaluate"),
never a false pass. **ERROR** is a separate technical state: if a stage or the database write fails, no decision
is reported or saved.

### Document-content checks (stage 2)

Deterministic, rule-based, no OCR, no LLM, no external API. Text is read with `pypdf` (first 5 pages), then:

| Step | What happens |
|---|---|
| Readable? | Fewer than 40 letters/digits, corrupted, password-protected or non-PDF → flagged, *manual verification required* |
| Images (PNG/JPG) | Accepted as uploads but **never** treated as content-checked → flagged, *manual verification required* |
| Type | Fixed keyword evidence (title phrases such as "certificate of incorporation", plus field labels such as "account holder"). Wrong or unrecognised type → *Document type mismatch* |
| Registration proof | Company name (`Registered Company Name:` / `Legal Name:` …) must match the form's legal name |
| Tax proof | Tax ID (`GSTIN:`, `Employer Identification Number (EIN):` …) must match the form (case/hyphen-normalized). Company name compared when present |
| Bank proof | Account holder must match the form's account holder; bank name and last four digits compared when present |

A match means *"the document is internally consistent with the form"*. It does **not** mean the document is
authentic, that a government registered the tax ID, or that the bank confirmed ownership.
Extracted text is trimmed, stripped of control characters/angle brackets, and always displayed escaped.
Uploaded bytes are read in memory for the run and **never stored**; history keeps only file names, sizes and results.

Scope, honestly: this works for **clearly structured, text-based PDFs** with `Label: value` lines like the samples.
Real-world certificates with other layouts may not be understood and will be sent to manual verification (Pending)
rather than approved. It has been exercised only against the fictional sample PDFs in this repository.

## What is and is not verified

- Tax checks are **format only**, not a registry lookup. Name comparison is **not** bank-ownership verification.
- Document matching is **not** authenticity verification.
- The denylist is a **fictional internal fixture**, not sanctions screening.
- **Approved** = passed these demo rules; not KYC, legal verification or payment authorization.
- Emails are **drafts, never sent**. The preview offers *Download draft (.txt)* and *Copy draft*. There is
  deliberately no "open in email app" button: `mailto:` hand-off depends on the visitor's configured mail client
  and opened a blank tab in testing, so it is not offered. (The `mailto` builder remains in `validation.py` and its test.)
- Run history is SQLite on the host: **shared by all visitors, may be lost on restart/redeploy**. Tax IDs are masked
  in the history table and the Run details dialog.

## Screens

- **Dashboard:** KPIs from saved records, recent-submissions table (select a row → *Run details* dialog with
  summary, processing trace and email draft), *Reset history* (confirmation dialog), *New vendor review*.
- **New Vendor Review:** form (two-column groupings), sample selector + matching sample-document downloads,
  a single **Live processing trace** (stages appear as they execute; collapses to a one-line summary afterwards and can
  be expanded), a **Result** card (decision, reasons, actions, masked submitted details), and a follow-up row with a
  *Preview email* button that opens the email dialog.
- **Footer** on both screens with a *Demo disclaimer* dialog.

## Demo scenarios (use *Reset history* first)

Pick the sample in **Load sample**, open **Sample documents**, download that scenario's PDFs, attach them to the matching
slots, click **Run checks**.

| ID | Sample | Expected |
|---|---|---|
| H1 | Northstar, all three PDFs | APPROVED |
| E1 | Bluefield, registration + bank only | PENDING, tax proof requested |
| E2 | Riverbend, all three | PENDING, manual review of the account-holder name (only that issue) |
| E3 | Westbridge (US), all three | PENDING, EIN format explained (only that issue) |
| E4 | Sample Restricted Vendor, all three | REJECTED, polite draft without internal detail |
| Wrong document | H1, but attach `northstar_registration_proof.pdf` in **Bank proof** | PENDING, *Document type mismatch* |
| Other tax ID | H1, but use `northstar_tax_proof_wrong_tax_id.pdf` | PENDING, *Tax ID mismatch* |
| No readable text | H1, but use `scanned_style_no_text.pdf` in any slot | PENDING, manual verification |
| Duplicate | Run H1 twice | second run PENDING, references the first |

## Deploy (GitHub + Streamlit Community Cloud)

```powershell
git init
git add .
git commit -m "VendorFlow case study"
git branch -M main
git remote add origin https://github.com/<you>/vendorflow.git
git push -u origin main
```

Then at share.streamlit.io: **Create app** → pick the repo, branch `main`, main file `app.py` → Deploy.
`requirements.txt` installs Streamlit and pypdf. `sample_docs/**/*.pdf` are committed; `*.db` is git-ignored, so
history starts empty on the host. Open the URL in a private window and run H1 before recording the demo.

## Assumptions / deliberate choices

- Duplicate check includes earlier **Pending** runs, so a corrected resubmission of the same vendor is flagged for
  manual review (intentional demo limitation).
- Denylist and duplicates are keyed on `(country, tax_id)`; the India denylist entry does not match a US submission.
- The bank document is compared with the *submitted account holder*; the legal-name vs account-holder difference is
  reported once, by the banking check (so E2 shows one issue, not two).
- An empty (0-byte) file is flagged. The sample selector fills text fields only; Streamlit cannot pre-fill uploaders.
- `.streamlit/config.toml` sets `maxUploadSize = 10` so an oversized file reaches the engine and is flagged by the 5 MB rule.

## Future improvements (not implemented)

OCR for scanned documents, resubmission linking, real email sending, more countries and document layouts,
checksum validation, role-based access.
