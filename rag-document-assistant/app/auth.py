"""Tenant-scoped service API keys; no plaintext credentials are stored."""

import hashlib
import secrets
import uuid
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db_session
from app.models import Tenant, TenantAPIKey


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def get_current_tenant_id(
    api_key: Annotated[str | None, Depends(api_key_header)],
    session: Annotated[Session, Depends(get_db_session)],
) -> uuid.UUID:
    """Resolve tenant identity only from a valid, active API key."""
    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    tenant_id = session.scalar(
        select(TenantAPIKey.tenant_id).where(
            TenantAPIKey.key_hash == hash_api_key(api_key),
            TenantAPIKey.is_active.is_(True),
        )
    )
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return tenant_id


def issue_api_key(session: Session, tenant_id: uuid.UUID, label: str) -> str:
    """Create one key and return its secret once for the operator to save."""
    if not label or len(label) > 100:
        raise ValueError("Key label must be between 1 and 100 characters")
    if session.get(Tenant, tenant_id) is None:
        raise ValueError("Unknown tenant ID")

    api_key = secrets.token_urlsafe(32)
    session.add(
        TenantAPIKey(
            tenant_id=tenant_id,
            label=label,
            key_hash=hash_api_key(api_key),
        )
    )
    session.commit()
    return api_key
