"""Create the initial document and credit-fact schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-08
"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import VECTOR
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def uuid_primary_key() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False)


def timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "tenants",
        uuid_primary_key(),
        sa.Column("name", sa.String(length=200), nullable=False),
        *timestamp_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
    )

    op.create_table(
        "borrowers",
        uuid_primary_key(),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("legal_name", sa.String(length=250), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        *timestamp_columns(),
        sa.CheckConstraint(
            "status IN ('active', 'watchlist', 'closed')",
            name=op.f("ck_borrowers_status_values"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_borrowers_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_borrowers")),
        sa.UniqueConstraint(
            "tenant_id",
            "external_id",
            name="uq_borrowers_tenant_external_id",
        ),
    )
    op.create_index(
        "ix_borrowers_tenant_name",
        "borrowers",
        ["tenant_id", "legal_name"],
    )

    op.create_table(
        "reporting_periods",
        uuid_primary_key(),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("borrower_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_type", sa.String(length=20), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=False),
        sa.Column("fiscal_quarter", sa.SmallInteger(), nullable=True),
        sa.Column("label", sa.String(length=100), nullable=False),
        *timestamp_columns(),
        sa.CheckConstraint(
            "fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4",
            name=op.f("ck_reporting_periods_fiscal_quarter_range"),
        ),
        sa.CheckConstraint(
            "period_type IN ('quarter', 'annual', 'ltm')",
            name=op.f("ck_reporting_periods_period_type_values"),
        ),
        sa.CheckConstraint(
            "period_end >= period_start",
            name=op.f("ck_reporting_periods_valid_date_range"),
        ),
        sa.ForeignKeyConstraint(
            ["borrower_id"],
            ["borrowers.id"],
            name=op.f("fk_reporting_periods_borrower_id_borrowers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_reporting_periods_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reporting_periods")),
        sa.UniqueConstraint(
            "borrower_id",
            "period_type",
            "period_end",
            name="uq_reporting_periods_borrower_type_end",
        ),
    )
    op.create_index(
        "ix_reporting_periods_tenant_borrower_end",
        "reporting_periods",
        ["tenant_id", "borrower_id", "period_end"],
    )

    op.create_table(
        "documents",
        uuid_primary_key(),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("borrower_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reporting_period_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "media_type",
            sa.String(length=100),
            server_default="application/pdf",
            nullable=False,
        ),
        sa.Column(
            "document_type",
            sa.String(length=20),
            server_default="filing",
            nullable=False,
        ),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        *timestamp_columns(),
        sa.CheckConstraint(
            "byte_size >= 0",
            name=op.f("ck_documents_byte_size_nonnegative"),
        ),
        sa.CheckConstraint(
            "document_type IN ('filing', 'supporting', 'other')",
            name=op.f("ck_documents_document_type_values"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name=op.f("ck_documents_status_values"),
        ),
        sa.ForeignKeyConstraint(
            ["borrower_id"],
            ["borrowers.id"],
            name=op.f("fk_documents_borrower_id_borrowers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reporting_period_id"],
            ["reporting_periods.id"],
            name=op.f("fk_documents_reporting_period_id_reporting_periods"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_documents_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint(
            "tenant_id",
            "content_hash",
            name="uq_documents_tenant_content_hash",
        ),
    )
    op.create_index(
        "ix_documents_borrower_period",
        "documents",
        ["borrower_id", "reporting_period_id"],
    )
    op.create_index(
        "ix_documents_tenant_status",
        "documents",
        ["tenant_id", "status"],
    )

    op.create_table(
        "document_pages",
        uuid_primary_key(),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "extraction_method",
            sa.String(length=100),
            server_default="pypdf",
            nullable=False,
        ),
        *timestamp_columns(),
        sa.CheckConstraint(
            "page_number > 0",
            name=op.f("ck_document_pages_page_number_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_pages_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_pages")),
        sa.UniqueConstraint(
            "document_id",
            "page_number",
            name="uq_document_pages_document_page_number",
        ),
    )

    op.create_table(
        "document_chunks",
        uuid_primary_key(),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("start_page", sa.Integer(), nullable=False),
        sa.Column("end_page", sa.Integer(), nullable=False),
        sa.Column("embedding", VECTOR(dim=768), nullable=False),
        sa.Column("embedding_model", sa.String(length=200), nullable=False),
        *timestamp_columns(),
        sa.CheckConstraint(
            "chunk_index >= 0",
            name=op.f("ck_document_chunks_chunk_index_nonnegative"),
        ),
        sa.CheckConstraint(
            "end_page >= start_page",
            name=op.f("ck_document_chunks_valid_page_range"),
        ),
        sa.CheckConstraint(
            "start_page > 0",
            name=op.f("ck_document_chunks_start_page_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint(
            "document_id",
            "chunk_index",
            name="uq_document_chunks_document_index",
        ),
    )
    op.create_index(
        "ix_document_chunks_embedding_hnsw",
        "document_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "financial_facts",
        uuid_primary_key(),
        sa.Column("borrower_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reporting_period_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_page_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("metric_name", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("unit", sa.String(length=50), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column(
            "is_canonical",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        *timestamp_columns(),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_financial_facts_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["borrower_id"],
            ["borrowers.id"],
            name=op.f("fk_financial_facts_borrower_id_borrowers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reporting_period_id"],
            ["reporting_periods.id"],
            name=op.f("fk_financial_facts_reporting_period_id_reporting_periods"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_page_id"],
            ["document_pages.id"],
            name=op.f("fk_financial_facts_source_page_id_document_pages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_financial_facts")),
    )
    op.create_index(
        "ix_financial_facts_borrower_period",
        "financial_facts",
        ["borrower_id", "reporting_period_id"],
    )
    op.create_index(
        "uq_financial_facts_canonical_metric",
        "financial_facts",
        ["borrower_id", "reporting_period_id", "metric_name"],
        unique=True,
        postgresql_where=sa.text("is_canonical"),
    )


def downgrade() -> None:
    op.drop_table("financial_facts")
    op.drop_table("document_chunks")
    op.drop_table("document_pages")
    op.drop_table("documents")
    op.drop_table("reporting_periods")
    op.drop_table("borrowers")
    op.drop_table("tenants")
    op.execute("DROP EXTENSION IF EXISTS vector")
