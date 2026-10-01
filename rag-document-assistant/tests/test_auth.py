import hashlib
import unittest
import uuid
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.api import app
from app.auth import hash_api_key
from app.database import get_db_session


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()

        def override_database_session():
            yield self.session

        app.dependency_overrides[get_db_session] = override_database_session
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_key_hash_is_deterministic_and_does_not_store_plaintext(self):
        api_key = "test-key"
        self.assertEqual(
            hash_api_key(api_key), hashlib.sha256(api_key.encode()).hexdigest()
        )
        self.assertNotEqual(hash_api_key(api_key), api_key)

    def test_protected_endpoints_reject_missing_key(self):
        requests = [
            lambda: self.client.post(
                "/ingest",
                data={
                    "borrower_id": str(uuid.uuid4()),
                    "reporting_period_id": str(uuid.uuid4()),
                },
                files={"files": ("test.pdf", b"%PDF-test", "application/pdf")},
            ),
            lambda: self.client.post(
                "/search", json={"queries": ["question"], "top_k": 5}
            ),
            lambda: self.client.post(
                "/answer", json={"query": "question", "top_k": 5}
            ),
            lambda: self.client.get("/collection-info"),
        ]
        for request in requests:
            with self.subTest(request=request):
                self.assertEqual(request().status_code, 401)
        self.session.scalar.assert_not_called()

    def test_invalid_key_is_rejected(self):
        self.session.scalar.return_value = None
        response = self.client.get(
            "/collection-info", headers={"X-API-Key": "invalid-key"}
        )
        self.assertEqual(response.status_code, 401)

    def test_valid_key_resolves_tenant_from_server_side_lookup(self):
        tenant_id = uuid.uuid4()
        self.session.scalar.side_effect = [tenant_id, 0, 0, 0]
        response = self.client.get(
            "/collection-info", headers={"X-API-Key": "valid-key"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tenant_id"], str(tenant_id))

    def test_health_endpoints_do_not_require_key(self):
        self.assertEqual(self.client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
