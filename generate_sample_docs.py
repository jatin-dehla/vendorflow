"""Regenerate the fictional sample PDFs in sample_docs/ (standard library only).

    python generate_sample_docs.py

Each scenario's documents are built from the same fields the form sample is filled with
(sample_data.SAMPLES), so correct uploads match the form. Nothing here decides an outcome.
"""
from pathlib import Path

import sample_data as sd

ROOT = Path(__file__).with_name("sample_docs")


def write(relative, data):
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print("wrote", relative, f"({len(data)} bytes)")


def main():
    for sample in sd.SAMPLES:
        pdfs = sd.document_pdfs(sample["fields"], sample["attach"])
        for slot, data in pdfs.items():
            write(sd.sample_doc_path(sample["id"], slot), data)

    h1 = next(s for s in sd.SAMPLES if s["id"] == "H1")["fields"]
    write("negative_examples/northstar_tax_proof_wrong_tax_id.pdf",
          sd.tax_pdf(h1["legal_name"], h1["country"], "27ZZZZZ9999Z1Z5"))
    write("negative_examples/northstar_registration_proof_wrong_company.pdf",
          sd.registration_pdf("Harborline Traders Pvt Ltd", h1["country"]))
    write("negative_examples/scanned_style_no_text.pdf", sd.build_pdf("", []))


if __name__ == "__main__":
    main()
