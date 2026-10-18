import csv
import io
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import Text, func, select, text

from app.api.serialization import serialize
from app.core.config import settings
from app.core.errors import Problem
from app.core.http import current_principal
from app.db.session import get_db
from app.importers.csv_importer import safe_csv_cell, suggest_mapping
from app.models import (
    STATES,
    AuditEvent,
    ImportJob,
    MappingTemplate,
    Product,
    RowValidationError,
    StagedRow,
)
from app.repositories.queries import page, scoped
from app.schemas.requests import (
    ApplyInput,
    Login,
    MappingInput,
    OrganizationUpdate,
    ProductCreate,
    ProductPatch,
    Register,
    TemplateCreate,
    TemplatePatch,
    valid_key,
)
from app.schemas.responses import (
    AuditOut,
    AuthOut,
    ErrorResponse,
    HealthOut,
    ImportOut,
    JobOut,
    OrganizationOut,
    PageOut,
    PreviewOut,
    PrincipalOut,
    ProductOut,
    RowOut,
    TemplateOut,
    UploadOut,
)
from app.services import auth, imports, inventory
from app.storage import s3
from app.tasks.celery_app import broker_ready

router = APIRouter(
    prefix="/api/v1",
    responses={
        code: {"model": ErrorResponse}
        for code in (400, 401, 403, 404, 409, 413, 422, 429, 500, 503)
    },
)
DB = Annotated[object, Depends(get_db)]
User = Annotated[auth.Principal, Depends(current_principal)]
Page = Annotated[int, Query(ge=1, le=100000)]
Size = Annotated[int, Query(ge=1, le=100)]
Search = Annotated[str, Query(max_length=200)]


def set_session(response, token):
    response.set_cookie(
        "ig_session",
        token,
        httponly=True,
        secure=settings().cookie_secure,
        samesite="lax",
        max_age=settings().session_hours * 3600,
        path="/",
    )


def job_response(job):
    return {
        "job_id": str(job.id),
        "import_id": str(job.import_id),
        "job_state": job.state,
        "status_url": f"/api/v1/imports/{job.import_id}",
    }


@router.post("/auth/register", status_code=201, response_model=AuthOut)
def register(data: Register, request: Request, response: Response, db: DB):
    token = auth.register(db, data, request.state.csrf)
    set_session(response, token)
    return {"user": auth.principal_for(db, token).__dict__}


@router.post("/auth/login", response_model=AuthOut)
def login(data: Login, request: Request, response: Response, db: DB):
    token = auth.login(
        db, data, request.state.csrf, request.client.host if request.client else "unknown"
    )
    set_session(response, token)
    return {"user": auth.principal_for(db, token).__dict__}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, db: DB, user: User):
    auth.logout(db, request.cookies["ig_session"])
    response.delete_cookie("ig_session", path="/")


@router.get("/auth/me", response_model=PrincipalOut)
def me(user: User):
    return user.__dict__


@router.get("/products", response_model=PageOut[ProductOut])
def products(db: DB, user: User, q: Search = "", page_number: Page = 1, page_size: Size = 25):
    return serialize(
        page(db, inventory.list_products(db, user.organization_id, q), page_number, page_size)
    )


@router.get("/products/{product_id}", response_model=ProductOut)
def product(product_id: UUID, db: DB, user: User):
    return serialize(scoped(db, Product, user.organization_id, product_id))


@router.post("/products", status_code=201, response_model=ProductOut)
def create_product(data: ProductCreate, db: DB, user: User):
    return serialize(inventory.write_product(db, user, data))


@router.patch("/products/{product_id}", response_model=ProductOut)
def update_product(product_id: UUID, data: ProductPatch, db: DB, user: User):
    return serialize(inventory.write_product(db, user, data, product_id))


@router.get("/products/{product_id}/history", response_model=PageOut[AuditOut])
def product_history(
    product_id: UUID, db: DB, user: User, page_number: Page = 1, page_size: Size = 25
):
    scoped(db, Product, user.organization_id, product_id)
    return serialize(
        page(
            db,
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == user.organization_id,
                AuditEvent.product_id == product_id,
            )
            .order_by(AuditEvent.created_at.desc()),
            page_number,
            page_size,
        )
    )


@router.post("/imports", status_code=201, response_model=UploadOut)
def upload_import(
    db: DB,
    user: User,
    name: Annotated[str, Form(min_length=1, max_length=200)],
    file: Annotated[UploadFile, File()],
):
    item, duplicate = imports.upload(db, user, file.filename, file.file, name)
    return {
        **serialize(item),
        "duplicate_import_id": str(duplicate) if duplicate else None,
        "suggested_mapping": suggest_mapping(item.headers),
    }


@router.get("/imports", response_model=PageOut[ImportOut])
def list_imports(
    db: DB,
    user: User,
    status: str | None = None,
    q: Search = "",
    page_number: Page = 1,
    page_size: Size = 25,
):
    query = select(ImportJob).where(ImportJob.organization_id == user.organization_id)
    if status:
        if status not in STATES:
            raise Problem("invalid_filter", "Unknown import status.", 422)
        query = query.where(ImportJob.status == status)
    if q:
        query = query.where(func.strpos(func.lower(ImportJob.name), q.lower()) > 0)
    return serialize(page(db, query.order_by(ImportJob.created_at.desc()), page_number, page_size))


@router.get("/imports/{import_id}", response_model=ImportOut)
def get_import(import_id: UUID, db: DB, user: User):
    return serialize(scoped(db, ImportJob, user.organization_id, import_id))


@router.put("/imports/{import_id}/mapping", response_model=ImportOut)
def mapping(import_id: UUID, data: MappingInput, db: DB, user: User):
    return serialize(imports.set_mapping(db, user, import_id, data.mapping))


@router.post("/imports/{import_id}/validate", status_code=202, response_model=JobOut)
def validate(import_id: UUID, db: DB, user: User):
    return job_response(imports.validate(db, user, import_id))


def staged_query(import_id, organization_id, q="", valid=None, action=None):
    query = select(StagedRow).where(
        StagedRow.import_id == import_id, StagedRow.organization_id == organization_id
    )
    if q:
        query = query.where(func.strpos(func.lower(StagedRow.original.cast(Text)), q.lower()) > 0)
    if valid is not None:
        query = query.where(StagedRow.valid == valid)
    if action:
        if action not in ("create", "update", "unchanged", "invalid"):
            raise Problem("invalid_filter", "Unknown proposed action.", 422)
        query = query.where(StagedRow.action == action)
    return query.order_by(StagedRow.record_number)


def rows_with_errors(db, result, organization_id):
    ids = [row.id for row in result["items"]]
    errors = (
        db.scalars(
            select(RowValidationError).where(
                RowValidationError.organization_id == organization_id,
                RowValidationError.row_id.in_(ids),
            )
        ).all()
        if ids
        else []
    )
    serialized = serialize(result)
    for item in serialized["items"]:
        item["errors"] = [serialize(e) for e in errors if str(e.row_id) == item["id"]]
    return serialized


@router.get("/imports/{import_id}/rows", response_model=PageOut[RowOut])
def rows(
    import_id: UUID,
    db: DB,
    user: User,
    q: Search = "",
    valid: bool | None = None,
    page_number: Page = 1,
    page_size: Size = 25,
):
    scoped(db, ImportJob, user.organization_id, import_id)
    return rows_with_errors(
        db,
        page(db, staged_query(import_id, user.organization_id, q, valid), page_number, page_size),
        user.organization_id,
    )


@router.get("/imports/{import_id}/preview", response_model=PreviewOut)
def preview(
    import_id: UUID,
    db: DB,
    user: User,
    action: str | None = None,
    q: Search = "",
    page_number: Page = 1,
    page_size: Size = 25,
):
    item = scoped(db, ImportJob, user.organization_id, import_id)
    result = rows_with_errors(
        db,
        page(
            db,
            staged_query(import_id, user.organization_id, q, action=action),
            page_number,
            page_size,
        ),
        user.organization_id,
    )
    return {
        **result,
        "status": item.status,
        "counts": item.counts,
        "preview_revision": item.preview_revision,
        "semantics": "Quantities replace stock. Missing SKUs remain. Imports never delete. Unmapped categories are preserved; mapped empty categories are cleared.",
    }


@router.post("/imports/{import_id}/refresh-preview", status_code=202, response_model=JobOut)
def refresh_preview(import_id: UUID, db: DB, user: User):
    return job_response(imports.refresh_preview(db, user, import_id))


@router.post("/imports/{import_id}/apply", status_code=202, response_model=JobOut)
def apply(
    import_id: UUID,
    data: ApplyInput,
    db: DB,
    user: User,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    try:
        valid_key(idempotency_key)
    except ValueError as exc:
        raise Problem("invalid_idempotency_key", str(exc), 422) from exc
    return job_response(
        imports.request_apply(
            db, user, import_id, idempotency_key, data.confirmed, data.preview_revision
        )
    )


@router.post("/imports/{import_id}/cancel", response_model=ImportOut)
def cancel(import_id: UUID, db: DB, user: User):
    return serialize(imports.cancel(db, user, import_id))


@router.post("/imports/{import_id}/retry", status_code=202, response_model=JobOut)
def retry(import_id: UUID, db: DB, user: User):
    return job_response(imports.retry(db, user, import_id))


@router.get("/imports/{import_id}/source")
def source(import_id: UUID, db: DB, user: User):
    item = scoped(db, ImportJob, user.organization_id, import_id)
    key, size = item.object_key, item.size
    db.rollback()  # Release database resources before external network I/O.
    body = s3.open_source(key)
    return StreamingResponse(
        s3.chunks(body),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="import-{import_id}.csv"',
            "Content-Length": str(size),
        },
    )


def error_report(db, organization_id, import_id):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["record_number", "field", "code", "message", "original_value"])
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    item = scoped(db, ImportJob, organization_id, import_id)
    if item.status == "INVALID" and item.failure_code:
        writer.writerow(
            [safe_csv_cell(v) for v in ("", "file", item.failure_code, item.failure_message, "")]
        )
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
    query = (
        select(StagedRow.record_number, RowValidationError)
        .join(RowValidationError, RowValidationError.row_id == StagedRow.id)
        .where(StagedRow.organization_id == organization_id, StagedRow.import_id == import_id)
        .order_by(StagedRow.record_number)
    )
    for number, error in db.execute(query.execution_options(yield_per=500)):
        writer.writerow(
            [
                safe_csv_cell(v)
                for v in (number, error.field, error.code, error.message, error.original_value)
            ]
        )
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)


@router.get("/imports/{import_id}/errors.csv")
def errors_csv(import_id: UUID, db: DB, user: User):
    scoped(db, ImportJob, user.organization_id, import_id)
    return StreamingResponse(
        error_report(db, user.organization_id, import_id),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="errors-{import_id}.csv"',
            "X-CSV-Escaping": "Formula-like cells are prefixed with an apostrophe; source values are preserved internally.",
        },
    )


@router.get("/mapping-templates", response_model=PageOut[TemplateOut])
def templates(db: DB, user: User, page_number: Page = 1, page_size: Size = 25):
    return serialize(
        page(
            db,
            select(MappingTemplate)
            .where(MappingTemplate.organization_id == user.organization_id)
            .order_by(MappingTemplate.name),
            page_number,
            page_size,
        )
    )


@router.post("/mapping-templates", status_code=201, response_model=TemplateOut)
def create_template(data: TemplateCreate, db: DB, user: User):
    return serialize(imports.save_template(db, user, data))


@router.patch("/mapping-templates/{template_id}", response_model=TemplateOut)
def update_template(template_id: UUID, data: TemplatePatch, db: DB, user: User):
    return serialize(imports.save_template(db, user, data, template_id))


@router.delete("/mapping-templates/{template_id}", status_code=204)
def delete_template(template_id: UUID, db: DB, user: User):
    user.require(*imports.EDITORS)
    db.delete(scoped(db, MappingTemplate, user.organization_id, template_id, lock=True))
    db.commit()


@router.get("/audit-events", response_model=PageOut[AuditOut])
def audit(db: DB, user: User, page_number: Page = 1, page_size: Size = 25):
    user.require("ADMIN")
    return serialize(
        page(
            db,
            select(AuditEvent)
            .where(AuditEvent.organization_id == user.organization_id)
            .order_by(AuditEvent.created_at.desc()),
            page_number,
            page_size,
        )
    )


@router.patch("/organization", response_model=OrganizationOut)
def configure_organization(data: OrganizationUpdate, db: DB, user: User):
    return serialize(inventory.configure_organization(db, user, data))


@router.get("/health/live", response_model=HealthOut)
def live():
    return {"status": "alive"}


@router.get("/health/ready", response_model=HealthOut)
def ready(db: DB):
    try:
        db.execute(text("SELECT 1 FROM alembic_version LIMIT 1"))
        broker_ready()
        s3.ready()
    except Exception as exc:
        raise Problem("not_ready", "A required dependency is unavailable.", 503) from exc
    return {"status": "ready"}
