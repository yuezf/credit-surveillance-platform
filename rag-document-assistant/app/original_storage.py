"""Durable storage for original PDFs, separate from derived database content."""

import os
import re
import shutil
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol


def pdf_object_key(tenant_id: uuid.UUID, content_hash: str) -> str:
    """Use only server-derived identity and file content to locate an original."""
    if re.fullmatch(r"[0-9a-f]{64}", content_hash) is None:
        raise ValueError("content_hash must be a lowercase SHA-256 digest")
    return f"{tenant_id}/{content_hash[:2]}/{content_hash}.pdf"


class OriginalPDFStore(Protocol):
    def put_pdf(
        self, file_path: Path, tenant_id: uuid.UUID, content_hash: str
    ) -> str: ...


class LocalOriginalPDFStore:
    """Development storage that survives API restarts on the same machine."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def put_pdf(
        self, file_path: Path, tenant_id: uuid.UUID, content_hash: str
    ) -> str:
        destination = self.root / pdf_object_key(tenant_id, content_hash)
        destination.parent.mkdir(parents=True, exist_ok=True)

        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
                temp_path = Path(temp.name)
                with file_path.open("rb") as source:
                    shutil.copyfileobj(source, temp)
            os.replace(temp_path, destination)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

        return destination.as_uri()


class S3OriginalPDFStore:
    """Production object storage using the ECS task role for credentials."""

    def __init__(self, bucket: str, client=None):
        if not bucket:
            raise ValueError("ORIGINAL_PDF_S3_BUCKET is required for S3 storage")
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.bucket = bucket
        self.client = client

    def put_pdf(
        self, file_path: Path, tenant_id: uuid.UUID, content_hash: str
    ) -> str:
        key = pdf_object_key(tenant_id, content_hash)
        with file_path.open("rb") as source:
            self.client.upload_fileobj(
                source,
                self.bucket,
                key,
                ExtraArgs={
                    "ContentType": "application/pdf",
                    "Metadata": {"sha256": content_hash},
                },
            )
        return f"s3://{self.bucket}/{key}"


def get_original_pdf_store() -> OriginalPDFStore:
    """Select local storage for development or S3 for a deployed service."""
    backend = os.getenv("ORIGINAL_PDF_STORAGE_BACKEND", "local").lower()
    if backend == "local":
        root = os.getenv("ORIGINAL_PDF_LOCAL_ROOT")
        if root is None:
            root = str(Path(__file__).resolve().parent.parent / "data" / "originals")
        return LocalOriginalPDFStore(Path(root))
    if backend == "s3":
        return S3OriginalPDFStore(os.getenv("ORIGINAL_PDF_S3_BUCKET", ""))
    raise ValueError("ORIGINAL_PDF_STORAGE_BACKEND must be local or s3")
