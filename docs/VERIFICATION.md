# Verification record

Verified locally on October 16, 2026 with Python 3.12.10, PostgreSQL 17.11, Redis 7.4, the pinned MinIO upstream source build, and the project's dependency lockfile.

## Automated checks

- `pytest --cov=app --cov-report=term-missing`: **63 passed**, **88% application coverage**. Tests use a dedicated PostgreSQL database, including concurrent worker connections. Storage is replaced with a deterministic in-memory adapter in this suite.
- `alembic check`: no schema drift.
- Ruff lint and formatting checks pass.
- One non-failing upstream Starlette warning reports that the stable HTTPX-based TestClient adapter is deprecated in favor of HTTPX2.

## Complete stack demonstration

`python -m scripts.demo` ran against the Compose web service, PostgreSQL, Redis/Celery and private MinIO:

1. An invalid source produced four invalid records and a downloadable escaped CSV report.
2. A corrected file contained 1,200 products.
3. A controlled worker exit occurred immediately after committed checkpoint **500**.
4. Inventory remained empty at that checkpoint.
5. Restarting the worker recovered the job and reached checkpoint **1,200** with no duplicate staged records.
6. Approval applied 1,200 products and 1,200 complete product audit entries.
7. Approval replay before and after completion returned the same apply job; products remained at version 1.

This is a correctness demonstration, not a throughput benchmark. Job delivery may be duplicated.

## Browser review

Checked using a temporary headless Microsoft Edge profile through the browser's debugging protocol:

- Desktop dashboard at 1440 × 1000 rendered correctly with the completed demo inventory.
- Mobile import review at 390 × 844 had no document-level horizontal overflow.
- An inventory search submitted through HTMX replaced the product table with the expected single result.
- Local Swagger UI loaded more than 20 documented operations; scripts were all served from the application origin.
- Screenshots were inspected visually. Temporary artifacts live under the ignored `.artifacts/` directory.

All six persistent Compose services reported healthy. Migration and bucket initialization services exited successfully. The optional seed command was checked separately to create two demo organizations with three roles each. Production demo seeding and failure injection remain disabled by default.
