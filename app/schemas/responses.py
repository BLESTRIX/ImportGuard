from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Output(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(Output):
    code: str
    message: str
    request_id: str


class PrincipalOut(Output):
    user_id: UUID
    organization_id: UUID
    role: str
    email: str
    organization_name: str
    currency: str


class AuthOut(Output):
    user: PrincipalOut


class Identified(Output):
    id: UUID
    created_at: datetime
    organization_id: UUID


class ProductOut(Identified):
    sku: str
    name: str
    category: str | None
    unit_price: str
    stock_quantity: int
    version: int
    updated_at: datetime


class PageOut[T](Output):
    items: list[T]
    total: int
    page: int
    page_size: int


class ImportOut(Identified):
    uploader_id: UUID
    name: str
    filename: str
    size: int
    checksum: str
    status: str
    headers: list[str]
    sample: list[list[str]]
    mapping: dict[str, str]
    frozen: bool
    checkpoint: int
    processed: int
    valid_count: int
    invalid_count: int
    counts: dict[str, int]
    preview_revision: UUID | None
    stage: str
    failure_stage: str | None
    failure_code: str | None
    failure_message: str | None
    progress_at: datetime
    approved_by: UUID | None
    applied_at: datetime | None


class UploadOut(ImportOut):
    duplicate_import_id: UUID | None
    suggested_mapping: dict[str, str]


class JobOut(Output):
    job_id: UUID
    import_id: UUID
    job_state: str
    status_url: str


class RowErrorOut(Identified):
    row_id: UUID
    field: str
    code: str
    message: str
    original_value: str


class RowOut(Identified):
    import_id: UUID
    record_number: int
    original: dict[str, Any]
    original_cells: list[str]
    normalized: dict[str, Any]
    sku: str | None
    valid: bool
    action: str | None
    expected_version: int | None
    previous: dict[str, Any] | None
    errors: list[RowErrorOut]


class PreviewOut(PageOut[RowOut]):
    status: str
    counts: dict[str, int]
    preview_revision: UUID | None
    semantics: str


class TemplateOut(Identified):
    name: str
    mapping: dict[str, str]


class AuditOut(Identified):
    product_id: UUID | None
    import_id: UUID | None
    actor_id: UUID
    event: str
    previous: dict[str, Any] | None
    new: dict[str, Any] | None


class HealthOut(Output):
    status: str


class OrganizationOut(Output):
    id: UUID
    created_at: datetime
    name: str
    currency: str
