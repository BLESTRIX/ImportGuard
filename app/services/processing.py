"""Database-owned jobs: Celery messages are wake-ups, never the source of truth."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.core.errors import InjectedFailure, LostLease, Problem
from app.db.session import session_factory
from app.importers.csv_importer import FieldError, normalize, records
from app.models import (
    AuditEvent,
    DurableJob,
    ImportJob,
    Product,
    RowValidationError,
    StagedRow,
    now,
)
from app.services.inventory import inventory_lock, product_values, snapshot
from app.storage import s3

logger = logging.getLogger(__name__)


class SimulatedWorkerCrash(BaseException):
    """Test/demo-only abrupt exit: deliberately bypass ordinary retry handling."""


@dataclass(frozen=True)
class Ownership:
    job_id: UUID
    import_id: UUID
    fence: int
    kind: str


def claim(db, job_id):
    reference = db.get(DurableJob, job_id)
    if reference is None:
        return None
    item = db.scalar(select(ImportJob).where(ImportJob.id == reference.import_id).with_for_update())
    job = db.scalar(
        select(DurableJob)
        .where(DurableJob.id == job_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    clock = db.scalar(select(func.clock_timestamp()))
    if not job or job.state not in ("PENDING", "RUNNING") or job.available_at > clock:
        db.rollback()
        return None
    if job.state == "RUNNING" and job.lease_until and job.lease_until > clock:
        db.rollback()
        return None
    if item.status in ("CANCELED", "APPLIED"):
        job.state = "CANCELED" if item.status == "CANCELED" else "DONE"
        db.commit()
        return None
    if job.attempts >= settings().max_job_attempts:
        job.state, item.status = "FAILED", "FAILED"
        item.stage = "failed"
        item.failure_code = "attempts_exhausted"
        item.failure_stage, item.failure_message = (
            job.kind,
            "Job exhausted its recovery attempts. Manual retry is available.",
        )
        db.commit()
        return None
    job.attempts += 1
    job.fence += 1
    job.state = "RUNNING"
    job.lease_until = clock + timedelta(seconds=settings().lease_seconds)
    item.status = "APPLYING" if job.kind == "APPLY" else "VALIDATING"
    item.stage = job.kind.lower()
    item.progress_at = clock
    result = Ownership(job.id, item.id, job.fence, job.kind)
    db.commit()
    return result


def guard(db, owner):
    # Fixed lock order: import first, durable job second. Cancellation uses it too.
    item = db.scalar(
        select(ImportJob)
        .where(ImportJob.id == owner.import_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = db.scalar(
        select(DurableJob)
        .where(DurableJob.id == owner.job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    clock = db.scalar(select(func.clock_timestamp()))
    if (
        job.state != "RUNNING"
        or job.fence != owner.fence
        or not job.lease_until
        or job.lease_until <= clock
        or item.status == "CANCELED"
    ):
        raise LostLease()
    job.lease_until = clock + timedelta(seconds=settings().lease_seconds)
    item.progress_at = clock
    return item, job


def add_errors(db, row, errors):
    if not errors:
        return
    db.flush()
    db.execute(
        insert(RowValidationError)
        .values(
            [
                {
                    "organization_id": row.organization_id,
                    "row_id": row.id,
                    "field": e.field,
                    "code": e.code,
                    "message": e.message,
                    "original_value": e.original_value,
                }
                for e in errors
            ]
        )
        .on_conflict_do_nothing(index_elements=["row_id", "field", "code"])
    )


def stage_batch(db, owner, batch, headers):
    item, _ = guard(db, owner)
    for number, cells in batch:
        if number <= item.checkpoint:
            continue
        original = {
            header: cells[index] if index < len(cells) else ""
            for index, header in enumerate(headers)
        }
        mapped = {field: original.get(column, "") for field, column in item.mapping.items()}
        normalized, errors = normalize(mapped, "category" in item.mapping)
        if len(cells) != len(headers):
            errors.append(
                FieldError(
                    "record",
                    "field_count",
                    f"Expected {len(headers)} fields, received {len(cells)}.",
                    repr(cells),
                )
            )
        row = StagedRow(
            organization_id=item.organization_id,
            import_id=item.id,
            record_number=number,
            original=original,
            original_cells=cells,
            normalized=normalized,
            sku=normalized.get("sku"),
            valid=not errors,
        )
        db.add(row)
        add_errors(db, row, errors)
        item.checkpoint = number
        item.processed += 1
        item.valid_count += not bool(errors)
        item.invalid_count += bool(errors)
    crash = settings().enable_failure_injection and item.failpoint == "crash_after_batch"
    if crash:
        item.failpoint = None
    item.stage = "staging"
    db.commit()  # Both checkpoint and staged rows commit, or neither does.
    if crash:
        raise SimulatedWorkerCrash("Stopped immediately after a committed staging batch")


def duplicate_check(db, item):
    duplicates = (
        select(StagedRow.sku)
        .where(StagedRow.import_id == item.id, StagedRow.sku.is_not(None))
        .group_by(StagedRow.sku)
        .having(func.count() > 1)
    )
    rows = db.scalars(
        select(StagedRow).where(StagedRow.import_id == item.id, StagedRow.sku.in_(duplicates))
    ).all()
    for row in rows:
        row.valid = False
        add_errors(
            db,
            row,
            [
                FieldError(
                    "sku",
                    "duplicate_sku",
                    "Every occurrence of this normalized SKU is invalid because it occurs more than once in the file.",
                    str(row.original.get(item.mapping["sku"], "")),
                )
            ],
        )
    db.flush()
    item.invalid_count = db.scalar(
        select(func.count())
        .select_from(StagedRow)
        .where(StagedRow.import_id == item.id, StagedRow.valid.is_(False))
    )
    item.valid_count = item.processed - item.invalid_count


def build_preview(db, item):
    inventory_lock(db, item.organization_id)
    rows = db.scalars(
        select(StagedRow).where(StagedRow.import_id == item.id).order_by(StagedRow.record_number)
    ).all()
    products = {
        p.sku: p
        for p in db.scalars(
            select(Product)
            .join(
                StagedRow,
                (StagedRow.sku == Product.sku)
                & (StagedRow.organization_id == Product.organization_id),
            )
            .where(StagedRow.import_id == item.id)
        ).all()
    }
    counts = {"create": 0, "update": 0, "unchanged": 0}
    for row in rows:
        if not row.valid:
            row.action = "invalid"
            continue
        product = products.get(row.sku)
        before = snapshot(product) if product else None
        row.expected_version = product.version if product else None
        row.previous = before
        if product is None:
            row.action = "create"
        elif any(before[k] != v for k, v in row.normalized.items()):
            row.action = "update"
        else:
            row.action = "unchanged"
        counts[row.action] += 1
    item.counts = counts
    item.preview_revision = uuid4()
    item.status = "INVALID" if item.invalid_count or not rows else "READY"
    item.stage = "complete"
    item.failure_message = "The CSV contains no data records." if not rows else None
    item.failure_stage = None
    item.failure_code = None
    item.progress_at = now()
    item.approved_by = None


def validate_file(db, owner):
    item, _ = guard(db, owner)
    key, checkpoint = item.object_key, item.checkpoint
    db.commit()  # No locks across object storage access or parser work.
    body = s3.open_source(key)
    iterator = None
    try:
        iterator = records(body)
        _, headers = next(iterator)
        batch = []
        for number, cells in iterator:
            if number <= checkpoint:
                continue
            batch.append((number, cells))
            if len(batch) >= settings().batch_size:
                stage_batch(db, owner, batch, headers)
                checkpoint = batch[-1][0]
                batch = []
        if batch:
            stage_batch(db, owner, batch, headers)
        item, job = guard(db, owner)
        item.stage = "duplicate_check"
        duplicate_check(db, item)
        build_preview(db, item)
        job.state = "DONE"
        job.lease_until = None
        db.commit()
    finally:
        if iterator is not None:
            iterator.close()
        body.close()


def refresh(db, owner):
    item, job = guard(db, owner)
    build_preview(db, item)
    job.state, job.lease_until = "DONE", None
    db.commit()


def apply_inventory(db, owner):
    item, _ = guard(db, owner)
    fail = settings().enable_failure_injection and item.failpoint == "apply_after_first_write"
    if fail:
        item.failpoint = None
    db.commit()  # Consume a one-shot failpoint outside the inventory transaction.
    item, job = guard(db, owner)
    inventory_lock(db, item.organization_id)
    rows = db.scalars(
        select(StagedRow).where(StagedRow.import_id == item.id).order_by(StagedRow.record_number)
    ).all()
    products = {
        p.sku: p
        for p in db.scalars(
            select(Product)
            .join(
                StagedRow,
                (StagedRow.sku == Product.sku)
                & (StagedRow.organization_id == Product.organization_id),
            )
            .where(StagedRow.import_id == item.id)
        ).all()
    }
    for row in rows:
        product = products.get(row.sku)
        if not row.valid or (product.version if product else None) != row.expected_version:
            item.status, item.stage = "STALE", "stale_preview"
            item.approved_by = None
            item.failure_message = (
                "Inventory changed after this preview. Regenerate it and approve again."
            )
            job.state, job.lease_until = "DONE", None
            db.commit()
            return
    changed = 0
    for row in rows:
        if row.action == "unchanged":
            continue
        product = products.get(row.sku)
        previous = snapshot(product) if product else None
        if product is None:
            product = Product(
                organization_id=item.organization_id, **product_values(row.normalized)
            )
            db.add(product)
        else:
            for key, value in product_values(row.normalized).items():
                setattr(product, key, value)
            product.version += 1
        db.flush()
        db.add(
            AuditEvent(
                organization_id=item.organization_id,
                product_id=product.id,
                import_id=item.id,
                actor_id=item.approved_by,
                event="import.product_" + row.action,
                previous=previous,
                new=snapshot(product),
            )
        )
        changed += 1
        if fail and changed == 1:
            raise InjectedFailure("Controlled failure after first inventory write")
    item.status, item.stage, item.applied_at = "APPLIED", "complete", now()
    item.failure_message, item.failure_stage = None, None
    item.failure_code = None
    item.progress_at = now()
    job.state, job.lease_until = "DONE", None
    db.add(
        AuditEvent(
            organization_id=item.organization_id,
            import_id=item.id,
            actor_id=item.approved_by,
            event="import.applied",
            new={"counts": item.counts, "job_id": str(job.id)},
        )
    )
    db.commit()  # Products, versions, audit entries and APPLIED are one transaction.


def record_failure(db, owner, error):
    db.rollback()
    try:
        item, job = guard(db, owner)
    except LostLease:
        db.rollback()
        return
    data_error = isinstance(error, Problem) and owner.kind == "VALIDATE"
    terminal = data_error or job.attempts >= settings().max_job_attempts
    message = (
        error.message
        if isinstance(error, Problem)
        else f"{type(error).__name__}: processing failed; see worker logs with job ID {job.id}."
    )
    item.failure_stage, item.failure_message = owner.kind, message
    item.failure_code = error.code if isinstance(error, Problem) else "processing_failed"
    job.last_error = message
    job.lease_until = None
    if data_error:
        job.state, item.status, item.stage = "DONE", "INVALID", "file_error"
    elif terminal:
        job.state, item.status, item.stage = "FAILED", "FAILED", "failed"
    else:
        job.state = "PENDING"
        job.available_at = now() + timedelta(seconds=min(300, 2**job.attempts))
        # APPLYING remains visible and noncancelable once its first attempt starts.
        item.status = "APPLYING" if owner.kind == "APPLY" else "QUEUED"
        item.stage = "retry_wait"
    db.commit()


def run_job(job_id):
    with session_factory()() as db:
        owner = claim(db, job_id)
        if not owner:
            return False
        try:
            if owner.kind == "VALIDATE":
                validate_file(db, owner)
            elif owner.kind == "PREVIEW":
                refresh(db, owner)
            else:
                apply_inventory(db, owner)
        except LostLease:
            db.rollback()
        except Exception as exc:
            logger.exception(
                "job_failed", extra={"job_id": str(owner.job_id), "import_id": str(owner.import_id)}
            )
            record_failure(db, owner, exc)
        return True


def discover(db):
    clock = func.now()
    return db.scalars(
        select(DurableJob.id)
        .where(
            ((DurableJob.state == "PENDING") & (DurableJob.available_at <= clock))
            | ((DurableJob.state == "RUNNING") & (DurableJob.lease_until <= clock))
        )
        .order_by(DurableJob.available_at)
        .limit(100)
    ).all()
