from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models import ImportJob, StagedRow, StorageObject, now
from app.services import processing
from app.services.imports import cleanup_orphans
from tests.integration.test_workflow import (
    MAPPING,
    VALID,
    approval,
    prepare,
    release_job,
    upload,
    validated,
)

pytestmark = pytest.mark.integration


def test_old_confirmation_cannot_approve_new_preview(admin):
    import_id = validated(admin)
    old_approval = approval(admin, import_id)
    job = admin.post(f"/api/v1/imports/{import_id}/refresh-preview").json()
    processing.run_job(UUID(job["job_id"]))
    response = admin.post(
        f"/api/v1/imports/{import_id}/apply",
        json=old_approval,
        headers={"Idempotency-Key": "old-confirmation"},
    )
    assert response.status_code == 409 and response.json()["code"] == "approval_stale"


def test_partial_uncommitted_batch_is_repeated_without_duplicate_rows(admin, db, monkeypatch):
    import_id, job_id = prepare(admin)
    real = processing.add_errors
    calls = 0

    def injected(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Controlled failure in the middle of an uncommitted batch")
        return real(*args)

    monkeypatch.setattr(processing, "add_errors", injected)
    processing.run_job(UUID(job_id))
    db.expire_all()
    assert db.get(ImportJob, UUID(import_id)).checkpoint == 0
    assert db.scalar(select(func.count()).select_from(StagedRow)) == 0
    monkeypatch.setattr(processing, "add_errors", real)
    release_job(db, job_id)
    processing.run_job(UUID(job_id))
    assert admin.get(f"/api/v1/imports/{import_id}").json()["status"] == "READY"
    assert db.scalar(select(func.count()).select_from(StagedRow)) == 2


def test_cleanup_never_deletes_referenced_objects_and_cleans_partial_uploads(
    admin, db, storage, monkeypatch
):
    item = upload(admin)
    referenced = db.scalar(select(StorageObject))
    referenced.created_at = now() - timedelta(days=2)
    orphan = StorageObject(
        key="sources/aborted-upload.csv",
        organization_id=referenced.organization_id,
        created_at=now() - timedelta(days=2),
    )
    db.add(orphan)
    db.commit()
    storage[orphan.key] = b"orphan"
    assert cleanup_orphans(db) == 1
    assert referenced.key in storage and orphan.key not in storage
    assert admin.get(f"/api/v1/imports/{item['id']}/source").content == VALID


def test_storage_failure_leaves_discoverable_reservation(admin, db, monkeypatch):
    from app.storage import s3

    def fail(*args):
        raise OSError("Injected storage failure")

    monkeypatch.setattr(s3, "put", fail)
    response = admin.post(
        "/api/v1/imports", data={"name": "partial"}, files={"file": ("a.csv", VALID)}
    )
    assert response.status_code == 500
    assert db.scalar(select(func.count()).select_from(ImportJob)) == 0
    assert db.scalar(select(StorageObject)).state == "PENDING"


def test_chunked_body_limit_is_enforced_without_content_length(admin, monkeypatch):
    monkeypatch.setattr(settings(), "max_upload_bytes", 10)
    response = admin.post(
        "/api/v1/imports",
        content=(b"x" * 10000 for _ in range(8)),
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 413, response.text


def test_malformed_late_records_field_counts_and_record_limit(admin, monkeypatch):
    # The malformed portion lies beyond the five-record upload sample.
    data = b"sku,name,price,qty\n" + b"a,Tea,1,1\n" * 5 + b'"unclosed,Tea,1,1\n'
    import_id = validated(admin, data)
    item = admin.get(f"/api/v1/imports/{import_id}").json()
    assert item["status"] == "INVALID" and "Malformed" in item["failure_message"]
    report = admin.get(f"/api/v1/imports/{import_id}/errors.csv").text
    assert "file,malformed_csv" in report
    import_id = validated(admin, b"sku,name,price,qty\na,Tea,1,1,extra\n")
    row = admin.get(f"/api/v1/imports/{import_id}/rows").json()["items"][0]
    assert row["original_cells"] == ["a", "Tea", "1", "1", "extra"]
    assert row["errors"][0]["code"] == "field_count"
    monkeypatch.setattr(settings(), "max_rows", 5)
    import_id = validated(admin, b"sku,name,price,qty\n" + b"a,Tea,1,1\n" * 6)
    assert admin.get(f"/api/v1/imports/{import_id}").json()["status"] == "INVALID"


def test_template_scope_and_new_file_validation(admin, other):
    template = admin.post(
        "/api/v1/mapping-templates", json={"name": "Supplier", "mapping": MAPPING}
    ).json()
    assert (
        other.patch(
            f"/api/v1/mapping-templates/{template['id']}", json={"name": "stolen"}
        ).status_code
        == 404
    )
    assert other.delete(f"/api/v1/mapping-templates/{template['id']}").status_code == 404
    item = upload(admin, b"code,title,amount,quantity\na,Tea,1,1\n")
    response = admin.put(
        f"/api/v1/imports/{item['id']}/mapping", json={"mapping": template["mapping"]}
    )
    assert response.status_code == 400 and response.json()["code"] == "missing_columns"


def test_cross_origin_and_session_bound_csrf(admin):
    assert (
        admin.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    admin.cookies.set("ig_csrf", "replacement", domain="testserver.local", path="/")
    assert (
        admin.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "replacement"}).status_code
        == 403
    )


def test_web_post_flows_use_shared_services(client):
    client.headers["Accept"] = "text/html"
    response = client.post(
        "/register",
        data={
            "csrf_token": client.cookies["ig_csrf"],
            "organization_name": "Web Shop",
            "currency": "USD",
            "email": "web@example.com",
            "password": "password123456",
        },
    )
    assert response.status_code == 200 and "Web Shop" in response.text
    response = client.post(
        "/inventory/new",
        data={
            "csrf_token": client.cookies["ig_csrf"],
            "sku": "web-1",
            "name": "Web product",
            "unit_price": "2.50",
            "stock_quantity": "5",
            "category": "Web",
        },
    )
    assert response.status_code == 200 and "Change history" in response.text
    response = client.post(
        "/imports/upload",
        data={"csrf_token": client.cookies["ig_csrf"], "name": "Web delivery"},
        files={"file": ("a.csv", VALID)},
    )
    assert response.status_code == 200 and "Connect your columns" in response.text


def test_openapi_describes_typed_responses_and_errors(client):
    schema = client.get("/openapi.json").json()
    response = schema["paths"]["/api/v1/products"]["get"]["responses"]
    assert "$ref" in response["200"]["content"]["application/json"]["schema"]
    docs = client.get("/docs").text
    assert "/static/vendor/swagger-ui-bundle.js" in docs
    assert "cdn.jsdelivr.net" not in docs
    assert response["403"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "ErrorResponse"
    )
