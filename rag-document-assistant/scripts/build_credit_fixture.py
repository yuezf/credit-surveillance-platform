"""Regenerate the checked-in synthetic PDF (development-only: needs reportlab)."""

import hashlib
import json
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "fixtures"
    manifest_path = root / "example_corp_credit.json"
    manifest = json.loads(manifest_path.read_text())
    output = root / "example_corp_credit.pdf"
    canvas = Canvas(str(output), pagesize=letter, invariant=1)
    canvas.setTitle("Example Corp - Synthetic Credit Case")
    canvas.setAuthor("Credit Surveillance Platform - synthetic fixture")
    for period in manifest["periods"]:
        canvas.setFillColor(HexColor("#15344b"))
        canvas.rect(0, 680, 612, 112, fill=1, stroke=0)
        canvas.setFillColor(HexColor("#ffffff"))
        canvas.setFont("Helvetica-Bold", 20)
        canvas.drawString(48, 740, "Example Corp | Credit Case")
        canvas.setFont("Helvetica", 12)
        canvas.drawString(48, 712, period["label"] + " - SYNTHETIC TRAINING DATA")
        canvas.setFillColor(HexColor("#15344b"))
        canvas.setFont("Helvetica-Bold", 13)
        canvas.drawString(48, 642, "Financial observations")
        y = 612
        for fact in period["facts"]:
            canvas.setFont("Helvetica", 12)
            canvas.drawString(48, y, fact["source_text"])
            y -= 27
        ttm = period["facts"][0]
        canvas.setFont("Helvetica", 10)
        canvas.drawString(48, y - 5, f'TTM window: {ttm["measurement_start"]} through {ttm["measurement_end"]}.')
        canvas.drawString(48, y - 23, "Debt is a point-in-time balance at that window's ending quarter-end.")
        canvas.drawString(48, y - 41, "Amounts are USD millions. EBITDA is unadjusted; debt is gross debt.")
        if period["page"] == 2:
            canvas.setFont("Helvetica-Bold", 13)
            canvas.drawString(48, 375, "Synthetic management explanation")
            canvas.setFont("Helvetica", 10)
            for index, line in enumerate(manifest["explanation"]):
                canvas.drawString(48, 345 - index * 20, line)
        canvas.setFont("Helvetica", 9)
        canvas.drawString(48, 55, "Fictional borrower. Not an actual filing or investment recommendation.")
        canvas.drawRightString(564, 55, f'Page {period["page"]} of 2')
        canvas.showPage()
    canvas.save()
    manifest["pdf_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
