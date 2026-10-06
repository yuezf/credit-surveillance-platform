# Recorded verification results

Recorded October 5, 2026. Evidence was checked through operator CLI responses,
non-secret task logs, local fixture hashing, and read-only account inspection.
This is a historical lab record, not a current uptime or production-readiness
claim. Account/resource addresses and secret values are omitted.

## Artifact and setup

- Source commit: `7b61385d3f5e472215d89d041761981fdd52fcd1`.
- Lab image: non-root Linux/amd64, includes Alembic configuration/migrations and
  public RDS CA bundle; no `.env` files or injected credentials.
- Recorded image digest:
  `sha256:028617874b0a76d8a9d6ef85d32f40d3715b77adafd5a3657a3cec556a7c15e1`.
  The prepared image was approximately 94 MB compressed. Its packaging included
  lab-specific HTTPS helpers outside the application source commit.
- Alembic head: `0002_tenant_api_keys`; vector extension `0.8.2`; column
  `vector(768)`.
- Migration, restricted-login bootstrap, key issuance, and read-only data check
  each completed with exit code 0.
- `ragapp` has no administrator flags, schema-create privilege, or API-key
  insertion permission. Temporary administrator-secret retrieval was removed.
- Model probes passed locally and from Fargate: finite nonzero 768-dimensional
  embeddings and a nonempty expected chat response.

## Endpoint results

| Check | Recorded result |
|---|---|
| Client-verified HTTPS `/health` | 200; `{"status":"ok"}` |
| Client-verified HTTPS `/health/ready` | 200; `{"status":"ready","database":"ok"}` |
| `/search` without key | 401; `Invalid or missing API key` |
| `/ingest` with demo key/scope | 200; `newly_ingested`; 1 text page; 2 chunks |
| `/search` financial question | Financial chunk ranked first |
| `/search` Harbor question | Operating-update chunk ranked first |
| `/answer` | 200; all requested fixture facts correct; sources/page 1 returned |

The missing-key check verifies one authentication path, not exhaustive tenant
isolation. The answer check covers one synthetic example, not an accuracy metric
across a representative dataset.

## Fixture and stored records

The [PDF](fixtures/synthetic-examplecorp-q2-2025.pdf) is fictional and contains no
real customer or personal data.

| Item | Recorded value |
|---|---|
| Bytes | 2289 |
| SHA-256 | `12bf527f984ed48038eadd2f42511ac1c9571944ae1980742204149a4c0f0179` |
| Document ID | `cf19b7fc-8af6-55da-8f0a-a79a4c657d56` |
| Chunk 0 | `5720baf9-0f03-5033-b2c5-26eea62342d9` |
| Chunk 1 | `b9b2cc5c-36fd-5c4d-b126-618a2119a9be` |
| Page metadata | Page 1 for both chunks |
| Original storage | Private S3 object; `application/pdf`; SSE-S3 `AES256` |

The object key follows `<tenant UUID>/<first two hash characters>/<hash>.pdf`.
The S3 metadata SHA-256 matched the fixture. More importantly, an authenticated
download's actual SHA-256 matched the local original. A read-only RDS check
confirmed the completed document, correct tenant/borrower/reporting period,
expected hash and S3 URI, one extracted page, two matching chunks, and finite
nonzero 768-dimensional vectors.

## Retrieval baseline

Both questions retrieved two chunks scoped to the same document, with the
maximum-distance cutoff disabled (`null`).

| Query | First chunk | First cosine distance | Second cosine distance |
|---|---|---:|---:|
| Revenue and adjusted EBITDA | 0 | 0.1644464233 | 0.4032692073 |
| Harbor delay and completion | 1 | 0.2796569896 | 0.4585112011 |

A lower cosine distance indicates a closer match. These are observations from a
smoke test, not threshold tuning or a general retrieval score.

## Answer example

The returned answer reported:

> Revenue: USD 12.4 million; adjusted EBITDA: USD 2.1 million; Harbor expansion
> delayed six weeks because synthetic equipment arrived late; planned completion
> September 2025; verification code HARBOR-768. Supporting page: 1.

The response attributed financial figures to Source 1, the delay to Sources 1/2,
and completion/code to Source 2. Source metadata included the correct document,
chunk indices, and page range. This is a condensed rendering of the response.

## Replacement evidence

- Before: task ID prefix `c0b2b0f8`; observed RUNNING/HEALTHY.
- After: task ID prefix `021d2a17`; different task; RUNNING/HEALTHY.
- Original task: STOPPED by ECS deployment activity.
- Replacement: different network interface and public IP; same API security group.
- Both health endpoints passed over verified HTTPS on the replacement.
- Existing API key and search requests returned HTTP 200 **without re-ingestion**.
- Document/hash, chunk IDs, stored text, rank order, and all four search distances
  matched the baseline.

This supports persistence across API-container replacement. S3 byte integrity
and direct database records were checked before replacement; the post-replacement
client check was health and retrieval. We did not perform a second S3 download,
a post-replacement answer test, database failover, backup recovery, or load test.

## Completion boundary

Completed: deployment, provider connectivity, migrations, secure key issuance,
health/authentication checks, synthetic ingestion/search/answer, S3 byte
verification, direct RDS inspection, and application-task persistence test.

Deferred: browser access and credit-surveillance agents.

Pending at this checkpoint: complete teardown and final Billing verification.
Do not describe the lab as fully cleaned up until that evidence is recorded.
