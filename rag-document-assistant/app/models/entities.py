import uuid
from datetime import date
from decimal import Decimal

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.schema_constants import DEFAULT_EMBEDDING_DIMENSION


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Borrower(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "borrowers"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "external_id",
            name="uq_borrowers_tenant_external_id",
        ),
        CheckConstraint(
            "status IN ('active', 'watchlist', 'closed')",
            name="status_values",
        ),
        Index("ix_borrowers_tenant_name", "tenant_id", "legal_name"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    legal_name: Mapped[str] = mapped_column(String(250), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="active",
    )


class ReportingPeriod(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reporting_periods"
    __table_args__ = (
        UniqueConstraint(
            "borrower_id",
            "period_type",
            "period_end",
            name="uq_reporting_periods_borrower_type_end",
        ),
        CheckConstraint(
            "period_type IN ('quarter', 'annual', 'ltm')",
            name="period_type_values",
        ),
        CheckConstraint("period_end >= period_start", name="valid_date_range"),
        CheckConstraint(
            "fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4",
            name="fiscal_quarter_range",
        ),
        Index(
            "ix_reporting_periods_tenant_borrower_end",
            "tenant_id",
            "borrower_id",
            "period_end",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    borrower_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("borrowers.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_type: Mapped[str] = mapped_column(String(20), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fiscal_quarter: Mapped[int | None] = mapped_column(SmallInteger)
    label: Mapped[str] = mapped_column(String(100), nullable=False)


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "content_hash",
            name="uq_documents_tenant_content_hash",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="status_values",
        ),
        CheckConstraint(
            "document_type IN ('filing', 'supporting', 'other')",
            name="document_type_values",
        ),
        CheckConstraint("byte_size >= 0", name="byte_size_nonnegative"),
        Index("ix_documents_tenant_status", "tenant_id", "status"),
        Index(
            "ix_documents_borrower_period",
            "borrower_id",
            "reporting_period_id",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    borrower_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("borrowers.id", ondelete="CASCADE"),
        nullable=False,
    )
    reporting_period_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("reporting_periods.id", ondelete="RESTRICT"),
        nullable=False,
    )
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        server_default="application/pdf",
    )
    document_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="filing",
    )
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_uri: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="pending",
    )


class DocumentPage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "page_number",
            name="uq_document_pages_document_page_number",
        ),
        CheckConstraint("page_number > 0", name="page_number_positive"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_method: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        server_default="pypdf",
    )


class DocumentChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "chunk_index",
            name="uq_document_chunks_document_index",
        ),
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonnegative"),
        CheckConstraint("start_page > 0", name="start_page_positive"),
        CheckConstraint("end_page >= start_page", name="valid_page_range"),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    start_page: Mapped[int] = mapped_column(Integer, nullable=False)
    end_page: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        VECTOR(DEFAULT_EMBEDDING_DIMENSION),
        nullable=False,
    )
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)


class FinancialFact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "financial_facts"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_range",
        ),
        Index(
            "ix_financial_facts_borrower_period",
            "borrower_id",
            "reporting_period_id",
        ),
        Index(
            "uq_financial_facts_canonical_metric",
            "borrower_id",
            "reporting_period_id",
            "metric_name",
            unique=True,
            postgresql_where=text("is_canonical"),
        ),
    )

    borrower_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("borrowers.id", ondelete="CASCADE"),
        nullable=False,
    )
    reporting_period_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("reporting_periods.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_page_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_pages.id", ondelete="SET NULL"),
    )
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3))
    source_text: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        nullable=True,
    )
    is_canonical: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
