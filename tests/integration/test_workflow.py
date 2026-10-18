import csv
import io
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import settings
from app.main import app
from app.models import AuditEvent, DurableJob, ImportJob, Membership, StagedRow, User, now
from app.services.auth import hasher
from app.services.processing import SimulatedWorkerCrash, run_job

pytestmark = pytest.mark.integration
MAPPING = {"sku": "sku", "name": "name", "unit_price": "price", "stock_quantity": "qty"}
VALID = b"sku,name,price,qty\nabc,Tea,1.25,4\nxyz,Coffee,3.50,7\n"


def upload(client, data=VALID):
    response = client.post(
        "/api/v1/imports",
        data={"name": "Supplier delivery"},
        files={"file": ("inventory.csv", data, "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def prepare(client, data=VALID, mapping=None):
    item = upload(client, data)
    response = client.put(
        f"/api/v1/imports/{item['id']}/mapping", json={"mapping": mapping or MAPPING}
    )
    assert response.status_code == 200, response.text
    response = client.post(f"/api/v1/imports/{item['id']}/validate")
    assert response.status_code == 202, response.text
    return item["id"], response.json()["job_id"]


def validated(client, data=VALID, mapping=None):
    import_id, job_id = prepare(client, data, mapping)
    run_job(UUID(job_id))
    return import_id


def apply(client, import_id, key="demo-key"):
    response = client.post(
        f"/api/v1/imports/{import_id}/apply",
        json=approval(client, import_id),
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def approval(client, import_id):
    return {
        "confirmed": True,
        "preview_revision": client.get(f"/api/v1/imports/{import_id}").json()["preview_revision"],
    }


def release_job(db, job_id):
    db.expire_all()
    job = db.get(DurableJob, UUID(job_id))
    job.available_at = now() - timedelta(seconds=1)
    job.lease_until = now() - timedelta(seconds=1)
    db.commit()


def test_auth_csrf_logout_and_server_side_hashed_sessions(client, db):
    from app.models import Session

    response = client.post(
        "/api/v1/auth/register",
        json={"email": "a@example.com", "password": "password123456", "organization_name": "A"},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert response.status_code == 403
    assert response.json()["request_id"]
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "a@example.com", "password": "password123456", "organization_name": "A"},
    )
    assert response.status_code == 201, response.text
    session = db.scalar(select(Session))
    assert session.token_hash != client.cookies["ig_session"]
    assert session.csrf_hash != client.cookies["ig_csrf"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401
    response = client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": "password123456"}
    )
    assert response.status_code == 200, response.text


def test_end_to_end_counts_audit_and_idempotency(admin, db):
    import_id = validated(admin)
    assert admin.get("/api/v1/products").json()["total"] == 0  # validation never writes inventory
    item = admin.get(f"/api/v1/imports/{import_id}").json()
    assert item["status"] == "READY", item
    assert item["counts"] == {"create": 2, "update": 0, "unchanged": 0}
    job_id = apply(admin, import_id)
    assert apply(admin, import_id) == job_id
    run_job(UUID(job_id))
    run_job(UUID(job_id))
    assert apply(admin, import_id, "different-key") == job_id
    products = admin.get("/api/v1/products").json()
    assert products["total"] == 2
    assert [p["version"] for p in products["items"]] == [1, 1]
    events = db.scalars(select(AuditEvent).where(AuditEvent.event.like("import.product_%"))).all()
    assert len(events) == 2
    assert all(e.import_id == UUID(import_id) and e.new["version"] == 1 for e in events)
    assert admin.get(f"/api/v1/imports/{import_id}").json()["status"] == "APPLIED"
    again = validated(admin)
    assert admin.get(f"/api/v1/imports/{again}").json()["counts"]["unchanged"] == 2
    response = admin.post(
        f"/api/v1/imports/{again}/apply",
        json=approval(admin, again),
        headers={"Idempotency-Key": "demo-key"},
    )
    assert response.status_code == 409


def test_tenant_isolation_across_resources_and_downloads(admin, other):
    import_id = validated(admin)
    job_id = apply(admin, import_id)
    run_job(UUID(job_id))
    product_id = admin.get("/api/v1/products").json()["items"][0]["id"]
    for path in (
        f"/products/{product_id}",
        f"/products/{product_id}/history",
        f"/imports/{import_id}",
        f"/imports/{import_id}/source",
        f"/imports/{import_id}/errors.csv",
        f"/imports/{import_id}/rows",
        f"/imports/{import_id}/preview",
    ):
        assert other.get("/api/v1" + path).status_code == 404, path
    assert other.get("/api/v1/products").json()["total"] == 0
    assert other.get("/api/v1/imports").json()["total"] == 0
    assert admin.get(f"/api/v1/imports/{import_id}/source").content == VALID


@pytest.mark.parametrize("role", ["EDITOR", "VIEWER"])
def test_roles_restrict_application_and_manual_writes(admin, db, role):
    import_id = validated(admin)
    org_id = UUID(admin.get("/api/v1/auth/me").json()["organization_id"])
    member = User(email=f"{role.lower()}@example.com", password_hash=hasher.hash("password123456"))
    db.add(member)
    db.flush()
    db.add(Membership(organization_id=org_id, user_id=member.id, role=role))
    db.commit()
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/login")
        client.headers["X-CSRF-Token"] = client.cookies["ig_csrf"]
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": member.email, "password": "password123456"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/imports/{import_id}/apply",
                json=approval(client, import_id),
                headers={"Idempotency-Key": "role-check"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/products",
                json={"sku": "x", "name": "X", "unit_price": "1.00", "stock_quantity": 0},
            ).status_code
            == 403
        )
        assert client.get(f"/api/v1/imports/{import_id}/source").status_code == 200
        assert client.get("/api/v1/audit-events").status_code == 403
        if role == "VIEWER":
            assert (
                client.post(
                    "/api/v1/imports",
                    data={"name": "forbidden"},
                    files={"file": ("test.csv", VALID)},
                ).status_code
                == 403
            )


def test_every_duplicate_across_batches_is_invalid_after_recovery(admin, db, monkeypatch):
    monkeypatch.setattr(settings(), "batch_size", 2)
    data = b"sku,name,price,qty\nabc,Tea,1,1\nx,Coffee,2,2\n AbC ,New Tea,3,3\n"
    import_id, job_id = prepare(admin, data)
    db.get(ImportJob, UUID(import_id)).failpoint = "crash_after_batch"
    db.commit()
    with pytest.raises(SimulatedWorkerCrash):
        run_job(UUID(job_id))
    db.expire_all()
    assert db.get(ImportJob, UUID(import_id)).checkpoint == 2
    assert db.scalar(select(func.count()).select_from(StagedRow)) == 2
    assert run_job(UUID(job_id)) is False  # unexpired lease prevents a competing worker
    release_job(db, job_id)
    run_job(UUID(job_id))
    item = admin.get(f"/api/v1/imports/{import_id}").json()
    assert item["status"] == "INVALID", item
    assert item["processed"] == 3 and item["invalid_count"] == 2 and item["valid_count"] == 1
    rows = admin.get(f"/api/v1/imports/{import_id}/rows").json()["items"]
    assert [r["record_number"] for r in rows if not r["valid"]] == [1, 3]
    assert all(r["errors"][0]["code"] == "duplicate_sku" for r in rows if not r["valid"])
    assert admin.get("/api/v1/products").json()["total"] == 0


def test_apply_failure_rolls_back_inventory_and_audit(admin, db):
    import_id = validated(admin)
    db.get(ImportJob, UUID(import_id)).failpoint = "apply_after_first_write"
    db.commit()
    job_id = apply(admin, import_id)
    run_job(UUID(job_id))
    assert admin.get("/api/v1/products").json()["total"] == 0
    db.expire_all()
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event.like("import.product_%"))
        )
        == 0
    )
    assert admin.post(f"/api/v1/imports/{import_id}/cancel").status_code == 409
    release_job(db, job_id)
    run_job(UUID(job_id))
    assert admin.get("/api/v1/products").json()["total"] == 2
    assert admin.get(f"/api/v1/imports/{import_id}").json()["status"] == "APPLIED"


def test_manual_edit_and_competing_import_make_previews_stale(admin):
    first = validated(admin)
    other = validated(admin)
    run_job(UUID(apply(admin, first, "first")))
    run_job(UUID(apply(admin, other, "other")))
    assert admin.get(f"/api/v1/imports/{other}").json()["status"] == "STALE"
    refresh = admin.post(f"/api/v1/imports/{other}/refresh-preview").json()
    run_job(UUID(refresh["job_id"]))
    product = admin.get("/api/v1/products").json()["items"][0]
    response = admin.patch(
        f"/api/v1/products/{product['id']}",
        json={"version": product["version"], "stock_quantity": 99},
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    run_job(UUID(apply(admin, other, "fresh")))
    assert admin.get(f"/api/v1/imports/{other}").json()["status"] == "STALE"


def test_categories_preserved_cleared_and_absolute_quantities(admin):
    response = admin.post(
        "/api/v1/products",
        json={
            "sku": "abc",
            "name": "Tea",
            "category": "Original",
            "unit_price": "1.25",
            "stock_quantity": 50,
        },
    )
    assert response.status_code == 201, response.text
    import_id = validated(admin)
    run_job(UUID(apply(admin, import_id)))
    product = admin.get("/api/v1/products?q=abc").json()["items"][0]
    assert product["category"] == "Original" and product["stock_quantity"] == 4
    cleared = validated(
        admin, b"sku,name,price,qty,category\nabc,Tea,1.25,0,\n", MAPPING | {"category": "category"}
    )
    run_job(UUID(apply(admin, cleared, "clear-category")))
    product = admin.get("/api/v1/products?q=abc").json()["items"][0]
    assert product["category"] is None and product["stock_quantity"] == 0
    assert admin.get("/api/v1/products").json()["total"] == 2  # Missing XYZ remains.


def test_error_report_escapes_original_formulas_and_preserves_internal_data(admin):
    import_id = validated(admin, b"sku,name,price,qty\n=evil,Tea,NaN,2.5\n")
    response = admin.get(f"/api/v1/imports/{import_id}/errors.csv")
    assert response.status_code == 200, response.text
    report = list(csv.DictReader(io.StringIO(response.text)))
    assert any(row["original_value"] == "'=evil" for row in report)
    rows = admin.get(f"/api/v1/imports/{import_id}/rows").json()["items"]
    assert rows[0]["original"]["sku"] == "=evil"
    assert (
        admin.post(
            f"/api/v1/imports/{import_id}/apply",
            json=approval(admin, import_id),
            headers={"Idempotency-Key": "invalid"},
        ).status_code
        == 409
    )


def test_upload_limits_headers_mapping_freeze_and_duplicate_warning(admin, monkeypatch):
    item = upload(admin)
    assert upload(admin)["duplicate_import_id"] == item["id"]
    response = admin.post(
        "/api/v1/imports", data={"name": "bad"}, files={"file": ("bad.csv", b"sku,SKU\na,b")}
    )
    assert response.status_code == 400 and response.json()["code"] == "duplicate_headers"
    import_id = validated(admin)
    assert (
        admin.put(f"/api/v1/imports/{import_id}/mapping", json={"mapping": MAPPING}).status_code
        == 409
    )
    monkeypatch.setattr(settings(), "max_upload_bytes", 10)
    response = admin.post(
        "/api/v1/imports", data={"name": "large"}, files={"file": ("large.csv", VALID)}
    )
    assert response.status_code == 413, response.text


def test_cancel_queued_validation_and_visible_terminal_failure(admin, db, monkeypatch):
    import_id, job_id = prepare(admin)
    assert admin.post(f"/api/v1/imports/{import_id}/cancel").status_code == 200
    assert run_job(UUID(job_id)) is False
    assert admin.get(f"/api/v1/imports/{import_id}").json()["status"] == "CANCELED"
    failed_id, failed_job = prepare(admin)
    monkeypatch.setattr(settings(), "max_job_attempts", 1)
    from app.storage import s3

    monkeypatch.setattr(
        s3, "open_source", lambda key: (_ for _ in ()).throw(OSError("storage unavailable"))
    )
    run_job(UUID(failed_job))
    item = admin.get(f"/api/v1/imports/{failed_id}").json()
    assert item["status"] == "FAILED" and item["failure_stage"] == "VALIDATE"
    assert admin.post(f"/api/v1/imports/{failed_id}/retry").status_code == 202


def test_web_pages_and_templates_render(admin):
    import_id = validated(admin)
    template = admin.post(
        "/api/v1/mapping-templates", json={"name": "Supplier", "mapping": MAPPING}
    )
    assert template.status_code == 201, template.text
    for path in (
        "/",
        "/inventory",
        "/inventory/new",
        "/imports",
        "/imports/upload",
        f"/imports/{import_id}",
        f"/imports/{import_id}/mapping",
        f"/imports/{import_id}/confirm",
        "/templates",
        "/audit",
        "/settings",
    ):
        response = admin.get(path, headers={"Accept": "text/html"})
        assert response.status_code == 200, (path, response.text)
        assert "ImportGuard" in response.text
    assert admin.get("/static/vendor/htmx.min.js").status_code == 200
    assert admin.get("/static/vendor/bootstrap.min.css").status_code == 200


def test_login_rate_limit(client):
    for _ in range(10):
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": "missing@example.com", "password": "wrong"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "missing@example.com", "password": "wrong"}
        ).status_code
        == 429
    )
