import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api.routes import (
    DB,
    Page,
    Search,
    Size,
    User,
    rows_with_errors,
    staged_query,
)
from app.core.errors import Problem
from app.importers.csv_importer import FIELDS, REQUIRED, suggest_mapping, validate_mapping
from app.models import STATES, AuditEvent, ImportJob, MappingTemplate, Product
from app.repositories.queries import page, scoped
from app.schemas.requests import (
    Login,
    OrganizationUpdate,
    ProductCreate,
    ProductPatch,
    Register,
    TemplateCreate,
    TemplatePatch,
)
from app.services import auth, imports, inventory

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "templates"))


def render(request, template, context=None, status=200):
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"csrf": getattr(request.state, "csrf", ""), **(context or {})},
        status_code=status,
    )


def redirect(request, path):
    if request.headers.get("hx-request"):
        from fastapi import Response

        return Response(status_code=200, headers={"HX-Redirect": path})
    return RedirectResponse(path, status_code=303)


def parse(schema, values):
    try:
        return schema.model_validate(values)
    except ValidationError as exc:
        raise Problem(
            "validation_error",
            "; ".join(".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors()),
            422,
        ) from exc


@router.get("/login")
@router.get("/register")
def auth_page(request: Request):
    return render(request, "auth.html", {"register": request.url.path == "/register"})


@router.get("/docs", include_in_schema=False)
def api_documentation(request: Request):
    return render(request, "api_docs.html")


@router.post("/login")
@router.post("/register")
async def authenticate(request: Request, db: DB):
    form = dict(await request.form())
    form.pop("csrf_token", None)
    registration = request.url.path == "/register"
    try:
        if registration:
            token = auth.register(db, parse(Register, form), request.state.csrf)
        else:
            token = auth.login(
                db,
                parse(Login, form),
                request.state.csrf,
                request.client.host if request.client else "unknown",
            )
    except Problem as exc:
        return render(
            request,
            "auth.html",
            {"register": registration, "error": exc.message, "email": form.get("email", "")},
            status=exc.status,
        )
    from app.api.routes import set_session

    response = redirect(request, "/")
    set_session(response, token)
    return response


@router.post("/logout")
def logout(request: Request, db: DB, user: User):
    auth.logout(db, request.cookies["ig_session"])
    response = redirect(request, "/login")
    response.delete_cookie("ig_session", path="/")
    return response


@router.get("/")
def dashboard(request: Request, db: DB, user: User):
    from datetime import timedelta

    from app.models import now

    cutoff = now() - timedelta(days=30)
    product_count = db.scalar(
        select(func.count())
        .select_from(Product)
        .where(Product.organization_id == user.organization_id)
    )
    statuses = dict(
        db.execute(
            select(ImportJob.status, func.count())
            .where(ImportJob.organization_id == user.organization_id)
            .group_by(ImportJob.status)
        ).all()
    )
    invalid = db.scalar(
        select(func.coalesce(func.sum(ImportJob.invalid_count), 0)).where(
            ImportJob.organization_id == user.organization_id, ImportJob.created_at >= cutoff
        )
    )
    recent = db.scalars(
        select(ImportJob)
        .where(ImportJob.organization_id == user.organization_id)
        .order_by(ImportJob.created_at.desc())
        .limit(6)
    ).all()
    successful = db.scalar(
        select(func.count())
        .select_from(ImportJob)
        .where(
            ImportJob.organization_id == user.organization_id,
            ImportJob.status == "APPLIED",
            ImportJob.applied_at >= cutoff,
        )
    )
    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "product_count": product_count,
            "statuses": statuses,
            "invalid": invalid,
            "successful": successful,
            "recent": recent,
        },
    )


@router.get("/inventory")
def products(
    request: Request,
    db: DB,
    user: User,
    q: Search = "",
    page_number: Page = 1,
    page_size: Size = 25,
):
    result = page(db, inventory.list_products(db, user.organization_id, q), page_number, page_size)
    fragment = request.headers.get("hx-target") == "inventory-table"
    return render(
        request,
        "_inventory_table.html" if fragment else "inventory.html",
        {"user": user, "result": result, "q": q},
    )


@router.get("/inventory/new")
def new_product(request: Request, user: User):
    user.require("ADMIN")
    return render(request, "product_form.html", {"user": user, "product": None})


@router.get("/inventory/{product_id}/edit")
def edit_product(product_id: uuid.UUID, request: Request, db: DB, user: User):
    user.require("ADMIN")
    return render(
        request,
        "product_form.html",
        {"user": user, "product": scoped(db, Product, user.organization_id, product_id)},
    )


@router.post("/inventory/new")
@router.post("/inventory/{product_id}/edit")
async def save_product(request: Request, db: DB, user: User, product_id: uuid.UUID | None = None):
    form = dict(await request.form())
    form.pop("csrf_token", None)
    data = parse(ProductPatch if product_id else ProductCreate, form)
    product = inventory.write_product(db, user, data, product_id)
    return redirect(request, f"/inventory/{product.id}")


@router.get("/inventory/{product_id}")
def product_detail(
    product_id: uuid.UUID, request: Request, db: DB, user: User, page_number: Page = 1
):
    item = scoped(db, Product, user.organization_id, product_id)
    history = page(
        db,
        select(AuditEvent)
        .where(
            AuditEvent.organization_id == user.organization_id, AuditEvent.product_id == product_id
        )
        .order_by(AuditEvent.created_at.desc()),
        page_number,
    )
    return render(request, "product.html", {"user": user, "product": item, "history": history})


@router.get("/imports")
def history(
    request: Request, db: DB, user: User, q: Search = "", status: str = "", page_number: Page = 1
):
    query = select(ImportJob).where(ImportJob.organization_id == user.organization_id)
    if status:
        if status not in STATES:
            raise Problem("invalid_filter", "Unknown import status.", 422)
        query = query.where(ImportJob.status == status)
    if q:
        query = query.where(func.strpos(func.lower(ImportJob.name), q.lower()) > 0)
    return render(
        request,
        "imports.html",
        {
            "user": user,
            "result": page(db, query.order_by(ImportJob.created_at.desc()), page_number),
            "q": q,
            "status": status,
            "states": STATES,
        },
    )


@router.get("/imports/upload")
def upload_page(request: Request, user: User):
    user.require(*imports.EDITORS)
    from app.core.config import settings

    return render(request, "upload.html", {"user": user, "limit": settings().max_upload_bytes})


@router.post("/imports/upload")
async def upload(request: Request, db: DB, user: User):
    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "file"):
        raise Problem("missing_file", "Choose a CSV file.", 422)
    # Run blocking storage/database work in the thread pool.
    from starlette.concurrency import run_in_threadpool

    item, duplicate = await run_in_threadpool(
        imports.upload, db, user, file.filename, file.file, str(form.get("name", ""))
    )
    suffix = f"?duplicate={duplicate}" if duplicate else ""
    return redirect(request, f"/imports/{item.id}/mapping{suffix}")


@router.get("/imports/{import_id}/mapping")
def mapping_page(
    import_id: uuid.UUID,
    request: Request,
    db: DB,
    user: User,
    duplicate: uuid.UUID | None = None,
    template_id: uuid.UUID | None = None,
):
    item = scoped(db, ImportJob, user.organization_id, import_id)
    mapping = item.mapping or suggest_mapping(item.headers)
    warning = None
    if duplicate:
        scoped(db, ImportJob, user.organization_id, duplicate)
    if template_id:
        template = scoped(db, MappingTemplate, user.organization_id, template_id)
        try:
            mapping = validate_mapping(template.mapping, item.headers)
        except Problem as exc:
            warning = exc.message
    saved = db.scalars(
        select(MappingTemplate)
        .where(MappingTemplate.organization_id == user.organization_id)
        .order_by(MappingTemplate.name)
    ).all()
    return render(
        request,
        "mapping.html",
        {
            "user": user,
            "item": item,
            "mapping": mapping,
            "fields": FIELDS,
            "required": REQUIRED,
            "saved": saved,
            "duplicate": duplicate,
            "warning": warning,
        },
    )


@router.post("/imports/{import_id}/mapping")
async def save_mapping(import_id: uuid.UUID, request: Request, db: DB, user: User):
    form = await request.form()
    mapping = {field: str(form.get(field, "")) for field in FIELDS}
    item = imports.set_mapping(db, user, import_id, mapping)
    name = str(form.get("template_name", "")).strip()
    if name:
        imports.save_template(
            db, user, parse(TemplateCreate, {"name": name, "mapping": item.mapping})
        )
    return redirect(request, f"/imports/{import_id}")


@router.get("/imports/{import_id}")
def import_detail(
    import_id: uuid.UUID,
    request: Request,
    db: DB,
    user: User,
    q: Search = "",
    action: str | None = None,
    page_number: Page = 1,
):
    item = scoped(db, ImportJob, user.organization_id, import_id)
    result = rows_with_errors(
        db,
        page(db, staged_query(import_id, user.organization_id, q, action=action), page_number),
        user.organization_id,
    )
    return render(
        request,
        "import.html",
        {"user": user, "item": item, "result": result, "q": q, "action": action or ""},
    )


@router.get("/imports/{import_id}/progress")
def progress(import_id: uuid.UUID, request: Request, db: DB, user: User):
    item = scoped(db, ImportJob, user.organization_id, import_id)
    response = render(request, "_progress.html", {"user": user, "item": item})
    if item.status not in ("QUEUED", "VALIDATING", "APPLY_QUEUED", "APPLYING"):
        response.headers["HX-Redirect"] = f"/imports/{import_id}"
    return response


@router.post("/imports/{import_id}/{operation}")
def import_operation(import_id: uuid.UUID, operation: str, request: Request, db: DB, user: User):
    handlers = {
        "validate": imports.validate,
        "cancel": imports.cancel,
        "retry": imports.retry,
        "refresh-preview": imports.refresh_preview,
    }
    handler = handlers.get(operation)
    if not handler:
        raise Problem("not_found", "Unknown operation.", 404)
    handler(db, user, import_id)
    return redirect(request, f"/imports/{import_id}")


@router.get("/imports/{import_id}/confirm")
def confirmation(import_id: uuid.UUID, request: Request, db: DB, user: User):
    user.require("ADMIN")
    item = scoped(db, ImportJob, user.organization_id, import_id)
    if item.status != "READY":
        raise Problem("not_ready", "This import is not ready to apply.", 409)
    return render(
        request, "confirm.html", {"user": user, "item": item, "idempotency_key": str(uuid.uuid4())}
    )


# Registered ahead of the generic operation route below during router installation.
@router.post("/imports/{import_id}/apply/confirmed")
async def apply_confirmed(import_id: uuid.UUID, request: Request, db: DB, user: User):
    form = await request.form()
    from app.schemas.requests import valid_key

    key = str(form.get("idempotency_key", ""))
    try:
        valid_key(key)
    except ValueError as exc:
        raise Problem("invalid_idempotency_key", str(exc), 422) from exc
    try:
        revision = uuid.UUID(str(form.get("preview_revision", "")))
    except ValueError as exc:
        raise Problem("approval_stale", "Reload and review the current preview.", 409) from exc
    imports.request_apply(db, user, import_id, key, form.get("confirmed") == "yes", revision)
    return redirect(request, f"/imports/{import_id}")


@router.get("/templates")
def mapping_templates(request: Request, db: DB, user: User, page_number: Page = 1):
    result = page(
        db,
        select(MappingTemplate)
        .where(MappingTemplate.organization_id == user.organization_id)
        .order_by(MappingTemplate.name),
        page_number,
    )
    return render(request, "templates.html", {"user": user, "result": result, "fields": FIELDS})


@router.post("/templates/{template_id}/{operation}")
async def manage_template(
    template_id: uuid.UUID, operation: str, request: Request, db: DB, user: User
):
    user.require(*imports.EDITORS)
    item = scoped(db, MappingTemplate, user.organization_id, template_id, lock=True)
    if operation == "delete":
        db.delete(item)
        db.commit()
    elif operation == "save":
        form = await request.form()
        imports.save_template(
            db,
            user,
            parse(
                TemplatePatch,
                {"name": form.get("name"), "mapping": {f: str(form.get(f, "")) for f in FIELDS}},
            ),
            template_id,
        )
    else:
        raise Problem("not_found", "Unknown template operation.", 404)
    return redirect(request, "/templates")


@router.get("/audit")
def audit(request: Request, db: DB, user: User, page_number: Page = 1):
    user.require("ADMIN")
    result = page(
        db,
        select(AuditEvent)
        .where(AuditEvent.organization_id == user.organization_id)
        .order_by(AuditEvent.created_at.desc()),
        page_number,
    )
    return render(request, "audit.html", {"user": user, "result": result})


@router.get("/settings")
def settings_page(request: Request, user: User):
    user.require("ADMIN")
    return render(request, "settings.html", {"user": user})


@router.post("/settings")
async def update_settings(request: Request, db: DB, user: User):
    form = await request.form()
    inventory.configure_organization(
        db,
        user,
        parse(OrganizationUpdate, {"name": form.get("name"), "currency": form.get("currency")}),
    )
    return redirect(request, "/settings")
