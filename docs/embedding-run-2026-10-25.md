# Real embedding smoke test — October 25, 2026

One synthetic document was indexed and one query searched through the configured
Gemini API. The user confirmed the API key's project showed Free tier in Google
AI Studio before the run. No billing settings or AWS resources were changed.

## Configuration and scope

- Encoder: `gemini-embedding-001`, 768 dimensions.
- Local database: `rag_credit_embedding_20261025_test`, newly created and migrated.
- Source: `example_corp_credit.pdf`, two stored pages and eight curated facts.
- Real vectors are separate from the earlier fake-vector demonstration database.
- The one-off run used a 30-second client timeout and disabled automatic retries.
- Three document chunks were embedded in one request; the query in a second.
- No chat model, agent loop, or finding-generation call was run.

## Observed results

Query: **Why did leverage deteriorate?**

Search used the existing maximum cosine distance of 0.6 and requested three
matches. All three chunks passed the cutoff:

| Rank | Source page | Cosine distance | Content |
| --- | --- | --- | --- |
| 1 | 2 | 0.431728 | Current financials and management explanation |
| 2 | 2 | 0.447414 | Overlapping tail including working-capital debt increase |
| 3 | 1 | 0.471906 | Prior financials |

The exact-page tool fetched pages 2 and 1 successfully. Page 2 contains the exact
quote `Lower customer volumes: USD 45 million reduction in EBITDA.` It also
attributes USD 25 million of the EBITDA reduction to unrecovered input costs
and the USD 100 million debt increase to working capital.

Stored page IDs/hashes and fact-to-page links matched before and after indexing.
The deterministic calculation still returned prior leverage 3.75x and current
leverage 4.848484...x, with a critical exception above 4.5x.

| Operation | Input | Provider-call wall time |
| --- | --- | --- |
| Document embedding | 3 chunks, 1,521 characters total | 0.729 seconds |
| Query embedding | 1 query, 29 characters | 0.206 seconds |

All four returned vectors had 768 dimensions. Total script wall time was 1.495
seconds, including local setup/database work. These are single observations,
not latency benchmarks. Character counts are not token counts.

## Limits and retained artifacts

This confirms that the real adapter, indexing, scoped retrieval, and exact-page
lookup work together for this one case. It does not establish broad retrieval
accuracy or an autonomous investigation. The second hit overlaps the first;
three hits here cover every chunk in this very small corpus.

The provider response contained no token-usage information. Actual billed cost
was not independently measured; Free-tier status was confirmed by the user.
Google AI Studio is the place to verify usage/billing afterward.

The local database and its index are retained for inspection. Matching indexing
inputs/model can reuse the index; further searches still embed their queries.
The detailed synthetic-only JSON report is at
`/private/tmp/credit-real-embedding-20261025.json`, the one-off script at
`/private/tmp/credit-real-embedding-run.py`, and source bytes at
`/private/tmp/credit-real-embedding-originals`. Temporary files may be cleared
by the operating system. No credentials were included in the report.
