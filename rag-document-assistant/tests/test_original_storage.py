import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from app.original_storage import (
    LocalOriginalPDFStore,
    S3OriginalPDFStore,
    get_original_pdf_store,
    pdf_object_key,
)


class OriginalPDFStorageTests(unittest.TestCase):
    def setUp(self):
        self.tenant_id = uuid.uuid4()
        self.pdf_bytes = b"%PDF-test-original"
        self.content_hash = hashlib.sha256(self.pdf_bytes).hexdigest()

    def test_object_key_is_tenant_scoped_and_deterministic(self):
        key = pdf_object_key(self.tenant_id, self.content_hash)
        self.assertEqual(
            key,
            f"{self.tenant_id}/{self.content_hash[:2]}/{self.content_hash}.pdf",
        )
        self.assertNotEqual(
            key, pdf_object_key(uuid.uuid4(), self.content_hash)
        )
        with self.assertRaises(ValueError):
            pdf_object_key(self.tenant_id, "../wrong")

    def test_local_store_preserves_original_bytes_across_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "upload.pdf"
            source.write_bytes(self.pdf_bytes)
            uri = LocalOriginalPDFStore(root / "originals").put_pdf(
                source, self.tenant_id, self.content_hash
            )
            self.assertEqual(
                uri,
                LocalOriginalPDFStore(root / "originals").put_pdf(
                    source, self.tenant_id, self.content_hash
                ),
            )
            self.assertEqual(
                Path(uri.removeprefix("file://")).read_bytes(), self.pdf_bytes
            )

    def test_s3_store_uses_tenant_scoped_key_and_pdf_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "upload.pdf"
            source.write_bytes(self.pdf_bytes)
            client = Mock()
            uri = S3OriginalPDFStore("private-bucket", client).put_pdf(
                source, self.tenant_id, self.content_hash
            )
            key = pdf_object_key(self.tenant_id, self.content_hash)
            self.assertEqual(uri, f"s3://private-bucket/{key}")
            self.assertEqual(client.upload_fileobj.call_args.args[1:], ("private-bucket", key))
            self.assertEqual(
                client.upload_fileobj.call_args.kwargs["ExtraArgs"],
                {"ContentType": "application/pdf", "Metadata": {"sha256": self.content_hash}},
            )

    def test_backend_selection_rejects_unknown_backend(self):
        with patch.dict("os.environ", {"ORIGINAL_PDF_STORAGE_BACKEND": "unknown"}):
            with self.assertRaises(ValueError):
                get_original_pdf_store()


if __name__ == "__main__":
    unittest.main()
