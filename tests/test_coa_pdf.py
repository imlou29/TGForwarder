"""Real Poppler/Tesseract checks with generated, non-user PDF fixtures."""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

# Reuse the platform-independent test loader for the indexer's Telegram adapter.
from test_coa_sync import sync

READY = all(shutil.which(x) for x in ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")) and importlib.util.find_spec("reportlab") and importlib.util.find_spec("PIL")


@unittest.skipUnless(READY, "PDF integration dependencies are installed by Linux CI")
class PDFTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "report.pdf"
    def tearDown(self):
        self.tmp.cleanup()

    def test_native_pdf_text_is_extracted(self):
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(self.path))
        for i, line in enumerate(["Lobster RT10 laboratory certificate", "Purity result 99.8 percent, quantity result 10.3 mg", "Identity verified by mass spectrometry and analytical chromatography", "Sample identifier TEST-ONLY-100; generated offline fixture"]):
            canvas.drawString(40, 780-i*24, line)
        canvas.save()
        text, status = sync.extract_pdf(self.path)
        self.assertEqual(status, "pdf_text")
        self.assertIn("99.8", text)
        self.assertIn("RT10", text)

    def test_scanned_pdf_uses_ocr(self):
        from PIL import Image, ImageDraw, ImageFont
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.lib.utils import ImageReader
        image = Image.new("RGB", (1600, 1800), "white")
        font = ImageFont.truetype("DejaVuSans.ttf", 52)
        draw = ImageDraw.Draw(image)
        for i, line in enumerate(["Lobster RT10", "Laboratory certificate", "Purity 99.8 percent", "Quantity 10.3 mg"]):
            draw.text((100, 150 + i*110), line, fill="black", font=font)
        canvas = Canvas(str(self.path))
        canvas.drawImage(ImageReader(image), 0, 0, width=595, height=670)
        canvas.save()
        text, status = sync.extract_pdf(self.path)
        self.assertEqual(status, "pdf_ocr_text")
        self.assertIn("RT10", text)
        self.assertIn("99.8", text)

    def test_page_cap_is_reported_as_partial(self):
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(self.path))
        for i in range(7):
            canvas.drawString(40, 780, "Lobster RT10 sample report for offline testing only. " * 3)
            canvas.showPage()
        canvas.save()
        text, status = sync.extract_pdf(self.path)
        self.assertEqual(status, "pdf_partial")
        self.assertIn("page 6", text)
        self.assertNotIn("page 7", text)

    def test_encrypted_pdf_is_not_processed(self):
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.lib.pdfencrypt import StandardEncryption
        canvas = Canvas(str(self.path), encrypt=StandardEncryption("", ownerPassword="fixture-only", canCopy=0))
        canvas.drawString(40, 780, "Restricted offline fixture")
        canvas.save()
        text, status = sync.extract_pdf(self.path)
        self.assertEqual((text, status), ("", "pdf_restricted"))


if __name__ == "__main__":
    unittest.main()
