import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def now():
    return datetime.now(UTC)


class Identity:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Organization(Identity, Base):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    __table_args__ = (CheckConstraint("currency ~ '^[A-Z]{3}$'"),)


class User(Identity, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)


class Membership(Base):
    __tablename__ = "memberships"
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (CheckConstraint("role IN ('ADMIN','EDITOR','VIEWER')"),)


class Session(Identity, Base):
    __tablename__ = "sessions"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "user_id"], ["memberships.organization_id", "memberships.user_id"]
        ),
    )


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Product(Identity, Base):
    __tablename__ = "products"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    stock_quantity: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("organization_id", "sku"),
        UniqueConstraint("organization_id", "id"),
        CheckConstraint("sku ~ '^[A-Z0-9_.-]{1,64}$'"),
        CheckConstraint("length(trim(name)) BETWEEN 1 AND 200"),
        CheckConstraint("unit_price >= 0"),
        CheckConstraint("stock_quantity BETWEEN 0 AND 1000000000"),
        CheckConstraint("version >= 1"),
        Index("ix_products_org_name", "organization_id", "name"),
    )


STATES = (
    "UPLOADED",
    "QUEUED",
    "VALIDATING",
    "INVALID",
    "READY",
    "APPLY_QUEUED",
    "APPLYING",
    "APPLIED",
    "STALE",
    "FAILED",
    "CANCELED",
)


class ImportJob(Identity, Base):
    __tablename__ = "imports"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    uploader_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    size: Mapped[int] = mapped_column(BigInteger)
    checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="UPLOADED")
    headers: Mapped[list] = mapped_column(JSONB, default=list)
    sample: Mapped[list] = mapped_column(JSONB, default=list)
    mapping: Mapped[dict] = mapped_column(JSONB, default=dict)
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    checkpoint: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    valid_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, default=0)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    preview_revision: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    stage: Mapped[str] = mapped_column(String(30), default="uploaded")
    failure_stage: Mapped[str | None] = mapped_column(String(30))
    failure_code: Mapped[str | None] = mapped_column(String(50))
    failure_message: Mapped[str | None] = mapped_column(Text)
    progress_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A one-shot development failpoint is consumed in a committed transaction.
    failpoint: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "object_key"],
            ["storage_objects.organization_id", "storage_objects.key"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "uploader_id"],
            ["memberships.organization_id", "memberships.user_id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "approved_by"],
            ["memberships.organization_id", "memberships.user_id"],
        ),
        CheckConstraint("status IN (" + ",".join(repr(s) for s in STATES) + ")"),
        Index("ix_imports_org_created", "organization_id", "created_at"),
        Index("ix_imports_org_checksum", "organization_id", "checksum"),
    )


class StorageObject(Base):
    __tablename__ = "storage_objects"
    key: Mapped[str] = mapped_column(String(500), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    state: Mapped[str] = mapped_column(String(12), default="PENDING")
    cleanup_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "key"),
        CheckConstraint("state IN ('PENDING','REFERENCED','DELETING')"),
    )


class StagedRow(Identity, Base):
    __tablename__ = "staged_rows"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    import_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    record_number: Mapped[int] = mapped_column(Integer)
    original: Mapped[dict] = mapped_column(JSONB)
    original_cells: Mapped[list] = mapped_column(JSONB, default=list)
    normalized: Mapped[dict] = mapped_column(JSONB)
    sku: Mapped[str | None] = mapped_column(String(64))
    valid: Mapped[bool] = mapped_column(Boolean, default=True)
    action: Mapped[str | None] = mapped_column(String(12))
    expected_version: Mapped[int | None] = mapped_column(Integer)
    previous: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "import_id"],
            ["imports.organization_id", "imports.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("import_id", "record_number"),
        UniqueConstraint("organization_id", "id"),
        CheckConstraint("record_number >= 1"),
        Index("ix_rows_import_sku", "import_id", "sku"),
        Index("ix_rows_import_valid_record", "import_id", "valid", "record_number"),
    )


class RowValidationError(Identity, Base):
    __tablename__ = "row_errors"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    row_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    field: Mapped[str] = mapped_column(String(40))
    code: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text)
    original_value: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "row_id"],
            ["staged_rows.organization_id", "staged_rows.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("row_id", "field", "code"),
    )


class MappingTemplate(Identity, Base):
    __tablename__ = "mapping_templates"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(100))
    mapping: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("organization_id", "name"),)


class DurableJob(Identity, Base):
    __tablename__ = "durable_jobs"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    import_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(15))
    state: Mapped[str] = mapped_column(String(15), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("organization_id", "import_id", "id", name="uq_jobs_org_import_id"),
        ForeignKeyConstraint(
            ["organization_id", "import_id"], ["imports.organization_id", "imports.id"]
        ),
        CheckConstraint("kind IN ('VALIDATE','PREVIEW','APPLY')"),
        CheckConstraint("state IN ('PENDING','RUNNING','DONE','FAILED','CANCELED')"),
        Index("ix_jobs_discovery", "state", "available_at", "lease_until"),
        Index(
            "uq_active_job_import",
            "import_id",
            unique=True,
            postgresql_where=text("state IN ('PENDING','RUNNING')"),
        ),
    )


class IdempotencyRecord(Identity, Base):
    __tablename__ = "idempotency_records"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    key: Mapped[str] = mapped_column(String(200))
    import_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("durable_jobs.id"))
    preview_generation: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "import_id", "job_id"],
            ["durable_jobs.organization_id", "durable_jobs.import_id", "durable_jobs.id"],
            name="fk_idempotency_job_scope",
        ),
        UniqueConstraint("organization_id", "key"),
        ForeignKeyConstraint(
            ["organization_id", "import_id"], ["imports.organization_id", "imports.id"]
        ),
    )


class AuditEvent(Identity, Base):
    __tablename__ = "audit_events"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    product_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    import_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    event: Mapped[str] = mapped_column(String(60))
    previous: Mapped[dict | None] = mapped_column(JSONB)
    new: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "product_id"], ["products.organization_id", "products.id"]
        ),
        ForeignKeyConstraint(
            ["organization_id", "import_id"], ["imports.organization_id", "imports.id"]
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_id"], ["memberships.organization_id", "memberships.user_id"]
        ),
        Index("ix_audit_org_created", "organization_id", "created_at"),
        Index("ix_audit_product_created", "product_id", "created_at"),
    )
