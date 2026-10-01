import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.ingestion import extract_pages_from_pdf


class PDFPageExtractionTests(unittest.TestCase):
    @patch("app.ingestion.PdfReader")
    def test_extracts_nonempty_pages_with_one_based_numbers(self, mock_reader):
        first_page = Mock()
        first_page.extract_text.return_value = " First page "
        blank_page = Mock()
        blank_page.extract_text.return_value = "  "
        third_page = Mock()
        third_page.extract_text.return_value = "Third page"
        mock_reader.return_value.pages = [first_page, blank_page, third_page]

        with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf:
            pages = extract_pages_from_pdf(pdf.name)

        self.assertEqual(
            [(page.page_number, page.text) for page in pages],
            [(1, "First page"), (3, "Third page")],
        )

    def test_rejects_non_pdf_files(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as file:
            with self.assertRaisesRegex(ValueError, "Only PDF"):
                extract_pages_from_pdf(file.name)

    def test_rejects_missing_files(self):
        missing_path = str(Path(tempfile.gettempdir()) / "missing-rag-document.pdf")

        with self.assertRaises(FileNotFoundError):
            extract_pages_from_pdf(missing_path)


if __name__ == "__main__":
    unittest.main()
