import hashlib
import tempfile
import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.core.config import settings
from app.core.errors import Problem
from app.importers.csv_importer import inspect_file, validate_mapping
from app.models import (
    AuditEvent,
    DurableJob,
    IdempotencyRecord,
    ImportJob,
    MappingTemplate,
    StorageObject,
    now,
)
from app.repositories.queries import scoped
from app.storage import s3

EDITORS = ("ADMIN", "EDITOR")


def upload(db, principal, filename, fileobj, name):
    principal.require(*EDITORS)
    name = name.strip()
    if not 1 <= len(name) <= 200:
        raise Problem("invalid_name", "Give the import a name of 1–200 characters.", 422)
    # Metadata never determines format. The strict parser determines compatibility.
    filename = (filename or "upload.csv").replace("\x00", "")[:255]
    digest, size = hashlib.sha256(), 0
    with tempfile.TemporaryFile(mode="w+b") as temporary:
        while chunk := fileobj.read(64 * 1024):
            size += len(chunk)
            if size > settings().max_upload_bytes:
                raise Problem("upload_limit", "The file exceeds the configured byte limit.", 413)
            digest.update(chunk)
            temporary.write(chunk)
        temporary.seek(0)
        headers, sample = inspect_file(temporary)
        temporary.seek(0)
        checksum = digest.hexdigest()
        duplicate = db.scalar(
            select(ImportJob.id)
            .where(
                ImportJob.organization_id == principal.organization_id,
                ImportJob.checksum == checksum,
            )
            .order_by(ImportJob.created_at.desc())
        )
        key = f"sources/{principal.organization_id}/{uuid.uuid4()}.csv"
        db.add(StorageObject(key=key, organization_id=principal.organization_id))
        db.commit()  # A reservation makes an interrupted upload discoverable for cleanup.
        try:
            s3.put(key, temporary, size)  # Never hold database locks during storage I/O.
            reservation = db.scalar(
                select(StorageObject).where(StorageObject.key == key).with_for_update()
            )
            if reservation.state != "PENDING":
                raise Problem(
                    "upload_expired", "The upload reservation expired. Upload the file again.", 409
                )
            item = ImportJob(
                organization_id=principal.organization_id,
                uploader_id=principal.user_id,
                name=name,
                filename=filename,
                object_key=key,
                size=size,
                checksum=checksum,
                headers=headers,
                sample=sample,
            )
            db.add(item)
            reservation.state = "REFERENCED"
            db.flush()
            db.add(
                AuditEvent(
                    organization_id=principal.organization_id,
                    import_id=item.id,
                    actor_id=principal.user_id,
                    event="import.uploaded",
                    new={"sha256": checksum, "size": size},
                )
            )
            db.commit()
            return item, duplicate
        except Exception:
            db.rollback()
            # Leave the reservation for the sweeper. An ambiguous commit must never
            # trigger an immediate delete of a possibly referenced object.
            raise


def set_mapping(db, principal, import_id, mapping):
    principal.require(*EDITORS)
    item = scoped(db, ImportJob, principal.organization_id, import_id, lock=True)
    if item.status != "UPLOADED" or item.frozen:
        raise Problem(
            "mapping_frozen",
            "Mapping is frozen once validation starts. Create a new import to change it.",
            409,
        )
    item.mapping = validate_mapping(mapping, item.headers)
    db.commit()
    return item


def enqueue(db, item, kind):
    job = DurableJob(organization_id=item.organization_id, import_id=item.id, kind=kind)
    db.add(job)
    db.flush()
    return job


def validate(db, principal, import_id):
    principal.require(*EDITORS)
    item = scoped(db, ImportJob, principal.organization_id, import_id, lock=True)
    if item.status != "UPLOADED":
        raise Problem("invalid_state", "Only a new upload can start validation.", 409)
    item.mapping = validate_mapping(item.mapping, item.headers)
    item.status, item.stage, item.frozen = "QUEUED", "queued", True
    job = enqueue(db, item, "VALIDATE")
    db.commit()
    return job


def refresh_preview(db, principal, import_id):
    principal.require(*EDITORS)
    item = scoped(db, ImportJob, principal.organization_id, import_id, lock=True)
    if item.status not in ("READY", "STALE"):
        raise Problem(
            "invalid_state", "Only a ready or stale import can regenerate its preview.", 409
        )
    item.status, item.stage = "QUEUED", "preview_queued"
    item.approved_by = None
    job = enqueue(db, item, "PREVIEW")
    db.commit()
    return job


def preview_generation(item):
    return str(item.preview_revision)


def request_apply(db, principal, import_id, key, confirmed, preview_revision):
    principal.require("ADMIN")
    if not confirmed:
        raise Problem(
            "confirmation_required",
            "Explicitly confirm the displayed create, update and unchanged counts.",
            422,
        )
    # Serialize keys across different imports too, using a transaction advisory lock.
    lock_id = int.from_bytes(
        hashlib.sha256(f"{principal.organization_id}:{key}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    db.execute(select(func.pg_advisory_xact_lock(lock_id)))
    item = scoped(db, ImportJob, principal.organization_id, import_id, lock=True)
    previous = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.organization_id == principal.organization_id,
            IdempotencyRecord.key == key,
        )
    )
    if previous:
        if previous.import_id != item.id or previous.preview_generation != str(preview_revision):
            raise Problem(
                "idempotency_conflict",
                "This key was already used for a different import or preview.",
                409,
            )
        return db.get(DurableJob, previous.job_id)
    if item.preview_revision != preview_revision:
        raise Problem(
            "approval_stale",
            "The preview changed. Review the current preview and approve it again.",
            409,
        )
    if item.status in ("APPLY_QUEUED", "APPLYING", "APPLIED"):
        existing_job = db.scalar(
            select(DurableJob)
            .where(DurableJob.import_id == item.id, DurableJob.kind == "APPLY")
            .order_by(DurableJob.created_at.desc())
        )
        db.add(
            IdempotencyRecord(
                organization_id=principal.organization_id,
                key=key,
                import_id=item.id,
                job_id=existing_job.id,
                preview_generation=preview_generation(item),
            )
        )
        db.commit()
        return existing_job
    if item.status != "READY" or item.invalid_count:
        raise Problem(
            "not_ready", "Only a READY import with no invalid records can be applied.", 409
        )
    item.status, item.stage, item.approved_by = "APPLY_QUEUED", "apply_queued", principal.user_id
    job = enqueue(db, item, "APPLY")
    db.add(
        IdempotencyRecord(
            organization_id=principal.organization_id,
            key=key,
            import_id=item.id,
            job_id=job.id,
            preview_generation=preview_generation(item),
        )
    )
    db.add(
        AuditEvent(
            organization_id=principal.organization_id,
            import_id=item.id,
            actor_id=principal.user_id,
            event="import.approved",
            new={"counts": item.counts, "job_id": str(job.id)},
        )
    )
    db.commit()
    return job


def cancel(db, principal, import_id):
    principal.require(*EDITORS)
    item = scoped(db, ImportJob, principal.organization_id, import_id, lock=True)
    if item.status in ("APPLYING", "APPLIED"):
        raise Problem(
            "apply_started", "Application has started; cancellation is no longer possible.", 409
        )
    if item.status == "CANCELED":
        return item
    jobs = db.scalars(
        select(DurableJob)
        .where(DurableJob.import_id == item.id, DurableJob.state.in_(("PENDING", "RUNNING")))
        .with_for_update()
    ).all()
    for job in jobs:
        job.state, job.fence = "CANCELED", job.fence + 1
    item.status, item.stage = "CANCELED", "canceled"
    db.commit()
    return item


def retry(db, principal, import_id):
    item = scoped(db, ImportJob, principal.organization_id, import_id, lock=True)
    principal.require(*(("ADMIN",) if item.failure_stage == "APPLY" else EDITORS))
    if item.status != "FAILED":
        raise Problem("invalid_state", "Only terminal failed jobs can be retried manually.", 409)
    if item.failure_stage == "APPLY":
        # Approval is not silently reused after terminal failure. Rebuild a preview.
        kind = "PREVIEW"
        item.approved_by = None
    else:
        kind = item.failure_stage or "VALIDATE"
    item.status, item.stage = "QUEUED", "retry_queued"
    item.failure_message = None
    job = enqueue(db, item, kind)
    db.commit()
    return job


def save_template(db, principal, data, template_id=None):
    principal.require(*EDITORS)
    template = (
        scoped(db, MappingTemplate, principal.organization_id, template_id, lock=True)
        if template_id
        else MappingTemplate(organization_id=principal.organization_id)
    )
    supplied = data.model_dump(exclude_unset=True)
    if "mapping" in supplied:
        supplied["mapping"] = validate_mapping(supplied["mapping"])
    for key, value in supplied.items():
        if value is None:
            raise Problem("invalid_template", "Template fields cannot be null.", 422)
        setattr(template, key, value)
    db.add(template)
    db.commit()
    return template


def cleanup_orphans(db):
    cutoff = now() - timedelta(hours=24)
    candidates = db.scalars(
        select(StorageObject)
        .where(StorageObject.state.in_(("PENDING", "DELETING")), StorageObject.created_at < cutoff)
        .order_by(StorageObject.cleanup_checked_at.asc().nulls_first(), StorageObject.created_at)
        .with_for_update(skip_locked=True)
        .limit(100)
    ).all()
    keys = []
    for obj in candidates:
        if not db.scalar(select(ImportJob.id).where(ImportJob.object_key == obj.key)):
            obj.state = "DELETING"
            obj.cleanup_checked_at = now()
            keys.append(obj.key)
    db.commit()
    for key in keys:
        s3.delete(key)
    # Retain DELETING tombstones, so an extremely late PUT is cleaned on the next pass.
    return len(keys)
