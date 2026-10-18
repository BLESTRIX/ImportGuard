from decimal import Decimal

from sqlalchemy import func, select

from app.core.errors import Problem
from app.importers.csv_importer import normalize
from app.models import AuditEvent, Organization, Product
from app.repositories.queries import scoped


def inventory_lock(db, organization_id):
    # A row lock is a PostgreSQL transaction lock with no hash-collision risk.
    # EVERY inventory writer and preview builder takes it before reading products.
    db.execute(select(Organization.id).where(Organization.id == organization_id).with_for_update())


def snapshot(product):
    return {
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "unit_price": format(product.unit_price, ".2f"),
        "stock_quantity": product.stock_quantity,
        "version": product.version,
    }


def product_values(values):
    return {k: Decimal(v) if k == "unit_price" else v for k, v in values.items()}


def write_product(db, principal, data, product_id=None):
    principal.require("ADMIN")
    inventory_lock(db, principal.organization_id)
    product = scoped(db, Product, principal.organization_id, product_id) if product_id else None
    before = snapshot(product) if product else None
    supplied = data.model_dump(exclude_unset=True)
    if product and supplied.pop("version") != product.version:
        raise Problem("version_conflict", "This product changed. Reload it before saving.", 409)
    raw = ({k: v for k, v in before.items() if k != "version"} if before else {}) | supplied
    normalized, errors = normalize(raw)
    if errors:
        raise Problem("invalid_product", "; ".join(f"{e.field}: {e.message}" for e in errors), 422)
    duplicate = db.scalar(
        select(Product.id).where(
            Product.organization_id == principal.organization_id, Product.sku == normalized["sku"]
        )
    )
    if duplicate and duplicate != product_id:
        raise Problem("duplicate_sku", "This SKU already exists in your organization.", 409)
    if product:
        for key, value in product_values(normalized).items():
            setattr(product, key, value)
        product.version += 1
    else:
        product = Product(organization_id=principal.organization_id, **product_values(normalized))
        db.add(product)
    db.flush()
    db.add(
        AuditEvent(
            organization_id=principal.organization_id,
            product_id=product.id,
            actor_id=principal.user_id,
            event="product.updated" if before else "product.created",
            previous=before,
            new=snapshot(product),
        )
    )
    db.commit()
    return product


def list_products(db, organization_id, q=""):
    query = select(Product).where(Product.organization_id == organization_id)
    if q:
        query = query.where(
            func.strpos(func.lower(Product.sku + " " + Product.name), q.lower()) > 0
        )
    return query.order_by(Product.sku)


def configure_organization(db, principal, data):
    from app.models import ImportJob

    principal.require("ADMIN")
    org = db.scalar(
        select(Organization).where(Organization.id == principal.organization_id).with_for_update()
    )
    if org.currency != data.currency and (
        db.scalar(select(Product.id).where(Product.organization_id == org.id).limit(1))
        or db.scalar(select(ImportJob.id).where(ImportJob.organization_id == org.id).limit(1))
    ):
        raise Problem(
            "currency_in_use", "Currency cannot change after inventory or imports exist.", 409
        )
    before = {"name": org.name, "currency": org.currency}
    org.name, org.currency = data.name, data.currency
    db.add(
        AuditEvent(
            organization_id=org.id,
            actor_id=principal.user_id,
            event="organization.updated",
            previous=before,
            new=data.model_dump(),
        )
    )
    db.commit()
    return org
