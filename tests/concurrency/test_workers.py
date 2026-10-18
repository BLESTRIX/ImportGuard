from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.core.errors import LostLease
from app.db.session import session_factory
from app.models import AuditEvent, DurableJob, Product, now
from app.services.processing import claim, guard, run_job
from tests.integration.test_workflow import apply, prepare, validated

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]


def test_competing_workers_stage_and_apply_only_once(admin, db):
    import_id, validation_job = prepare(admin)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run_job, [UUID(validation_job)] * 4))
    assert admin.get(f"/api/v1/imports/{import_id}").json()["processed"] == 2
    apply_job = apply(admin, import_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run_job, [UUID(apply_job)] * 4))
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Product)) == 2
    assert (
        db.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.event == "import.applied")
        )
        == 1
    )


def test_expired_owners_are_fenced_out(admin, db):
    import_id, job_id = prepare(admin)
    with session_factory()() as first:
        owner1 = claim(first, UUID(job_id))
    job = db.get(DurableJob, UUID(job_id))
    job.lease_until = now() - timedelta(seconds=1)
    db.commit()
    with session_factory()() as second:
        owner2 = claim(second, UUID(job_id))
        assert owner2.fence > owner1.fence
    with session_factory()() as first, pytest.raises(LostLease):
        guard(first, owner1)


def test_two_imports_for_same_org_serialize_and_one_becomes_stale(admin):
    imports = [validated(admin), validated(admin)]
    jobs = [UUID(apply(admin, item, f"key-{index}")) for index, item in enumerate(imports)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run_job, jobs))
    states = [admin.get(f"/api/v1/imports/{item}").json()["status"] for item in imports]
    assert sorted(states) == ["APPLIED", "STALE"]
