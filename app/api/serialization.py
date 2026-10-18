from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import inspect


def serialize(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Decimal):
        return format(value, ".2f")
    if isinstance(value, (UUID, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(v) for v in value]
    # Only explicit public model columns; never return credentials or storage keys.
    hidden = {"password_hash", "token_hash", "csrf_hash", "object_key", "failpoint"}
    return {
        column.key: serialize(getattr(value, column.key))
        for column in inspect(value).mapper.column_attrs
        if column.key not in hidden
    }
