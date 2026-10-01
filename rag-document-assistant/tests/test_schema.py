import unittest

from app.models import Base, DocumentChunk, FinancialFact


EXPECTED_TABLES = {
    "tenants",
    "tenant_api_keys",
    "borrowers",
    "reporting_periods",
    "documents",
    "document_pages",
    "document_chunks",
    "financial_facts",
}


class SchemaMetadataTests(unittest.TestCase):
    def test_minimal_table_set_is_registered(self):
        self.assertEqual(set(Base.metadata.tables), EXPECTED_TABLES)

    def test_tenant_scope_is_kept_only_where_needed(self):
        directly_scoped_tables = {
            "tenant_api_keys", "borrowers", "reporting_periods", "documents"
        }
        indirectly_scoped_tables = {
            "document_pages",
            "document_chunks",
            "financial_facts",
        }

        for table_name in directly_scoped_tables:
            with self.subTest(table=table_name):
                self.assertIn("tenant_id", Base.metadata.tables[table_name].c)

        for table_name in indirectly_scoped_tables:
            with self.subTest(table=table_name):
                self.assertNotIn("tenant_id", Base.metadata.tables[table_name].c)

    def test_document_carries_credit_identity(self):
        document_columns = Base.metadata.tables["documents"].c

        self.assertIn("tenant_id", document_columns)
        self.assertIn("borrower_id", document_columns)
        self.assertIn("reporting_period_id", document_columns)
        self.assertNotIn("case_id", document_columns)

    def test_document_chunk_contains_only_current_retrieval_fields(self):
        chunk_columns = set(DocumentChunk.__table__.c.keys())

        self.assertTrue(
            {
                "document_id",
                "chunk_index",
                "text_content",
                "start_page",
                "end_page",
                "embedding",
                "embedding_model",
            }.issubset(chunk_columns)
        )
        self.assertTrue(
            {
                "tenant_id",
                "ingestion_version",
                "page_numbers",
                "start_char",
                "end_char",
                "chunk_size",
                "chunk_overlap",
                "embedding_dimension",
            }.isdisjoint(chunk_columns)
        )

    def test_document_chunk_vector_dimension_is_768(self):
        embedding_type = DocumentChunk.__table__.c.embedding.type
        self.assertEqual(embedding_type.dim, 768)

    def test_document_chunk_uses_cosine_hnsw_index(self):
        index = next(
            index
            for index in DocumentChunk.__table__.indexes
            if index.name == "ix_document_chunks_embedding_hnsw"
        )

        self.assertEqual(index.dialect_options["postgresql"]["using"], "hnsw")
        self.assertEqual(
            index.dialect_options["postgresql"]["ops"],
            {"embedding": "vector_cosine_ops"},
        )

    def test_financial_fact_keeps_optional_confidence(self):
        confidence = FinancialFact.__table__.c.confidence
        self.assertTrue(confidence.nullable)

    def test_financial_fact_defaults_to_noncanonical(self):
        is_canonical = FinancialFact.__table__.c.is_canonical
        self.assertFalse(is_canonical.nullable)
        self.assertEqual(str(is_canonical.server_default.arg), "false")

    def test_only_canonical_facts_must_be_unique(self):
        index = next(
            index
            for index in FinancialFact.__table__.indexes
            if index.name == "uq_financial_facts_canonical_metric"
        )

        self.assertTrue(index.unique)
        self.assertEqual(
            [expression.name for expression in index.expressions],
            ["borrower_id", "reporting_period_id", "metric_name"],
        )
        self.assertEqual(
            str(index.dialect_options["postgresql"]["where"]),
            "is_canonical",
        )


if __name__ == "__main__":
    unittest.main()
