from sqlalchemy import func, select

from app.core.errors import Problem


def scoped(db, model, organization_id, object_id, *, lock=False):
    query = select(model).where(model.organization_id == organization_id, model.id == object_id)
    if lock:
        query = query.with_for_update()
    result = db.scalar(query.execution_options(populate_existing=True))
    if result is None:
        raise Problem("not_found", "The requested resource was not found.", 404)
    return result


def page(db, query, page_number=1, page_size=25):
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    items = db.scalars(query.offset((page_number - 1) * page_size).limit(page_size)).all()
    return {"items": items, "total": total, "page": page_number, "page_size": page_size}
