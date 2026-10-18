import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import BinaryIO

from app.core.config import settings
from app.core.errors import Problem

FIELDS = ("sku", "name", "unit_price", "stock_quantity", "category")
REQUIRED = FIELDS[:4]
ALIASES = {
    "sku": ("sku", "product_code", "product code", "item_code"),
    "name": ("name", "product_name", "product name", "description"),
    "unit_price": ("unit_price", "price", "unit price"),
    "stock_quantity": ("stock_quantity", "qty", "quantity", "stock"),
    "category": ("category", "department"),
}
ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


@dataclass(frozen=True)
class FieldError:
    field: str
    code: str
    message: str
    original_value: str


def normalize(values: dict, category_mapped: bool = True) -> tuple[dict, list[FieldError]]:
    """Normalize text, but never round prices or truncate quantities."""
    result, errors = {}, []
    for field in FIELDS:
        if field == "category" and not category_mapped:
            continue
        original = str(values.get(field, "") if values.get(field) is not None else "")
        value = original.strip()
        code, message = None, ""
        if field == "sku":
            value = value.translate(ASCII_UPPER)
            if not re.fullmatch(r"[A-Z0-9_.-]{1,64}", value):
                code, message = (
                    "invalid_sku",
                    "Use 1–64 ASCII letters, digits, hyphens, underscores or periods.",
                )
        elif field in ("name", "category"):
            limit = 200 if field == "name" else 100
            if (field == "name" and not value) or len(value) > limit or "\x00" in value:
                code, message = (
                    "invalid_text",
                    f"{field} must contain {'1' if field == 'name' else '0'}–{limit} characters without NUL.",
                )
            if field == "category" and value == "":
                value = None
        elif field == "unit_price":
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value) or len(value) > 32:
                code, message = (
                    "invalid_price",
                    "Use a nonnegative decimal with at most two decimal places; no symbols, separators or exponents.",
                )
            elif Decimal(value) > Decimal("9999999999.99"):
                code, message = "price_out_of_range", "Price must fit NUMERIC(12,2)."
            else:
                value = format(Decimal(value), ".2f")
        elif field == "stock_quantity":
            if not re.fullmatch(r"[0-9]+", value) or len(value) > 16:
                code, message = (
                    "invalid_quantity",
                    "Quantity must be a whole number from 0 to 1,000,000,000.",
                )
            elif int(value) > 1000000000:
                code, message = "quantity_out_of_range", "Quantity exceeds 1,000,000,000."
            else:
                value = int(value)
        if not original.strip() and field in REQUIRED:
            code, message = "required", f"{field} is required."
        if code:
            errors.append(FieldError(field, code, message, original))
        else:
            result[field] = value
    return result, errors


def validate_mapping(mapping: dict, headers: list[str] | None = None) -> dict:
    cleaned = {k: v for k, v in mapping.items() if v not in (None, "")}
    if set(cleaned) - set(FIELDS) or set(REQUIRED) - set(cleaned):
        raise Problem(
            "invalid_mapping", "Map SKU, name, unit price and stock quantity; category is optional."
        )
    if any(not isinstance(v, str) for v in cleaned.values()):
        raise Problem("invalid_mapping", "Column names must be strings.")
    if len(set(cleaned.values())) != len(cleaned):
        raise Problem("ambiguous_mapping", "A source column cannot map to more than one field.")
    if headers is not None and set(cleaned.values()) - set(headers):
        raise Problem("missing_columns", "The mapping references columns absent from this file.")
    return cleaned


def suggest_mapping(headers: list[str]) -> dict:
    # Ambiguous aliases deliberately produce no suggestion for that field.
    return {
        field: matches[0]
        for field, aliases in ALIASES.items()
        if len(matches := [h for h in headers if h.strip().lower() in aliases]) == 1
    }


def records(stream: BinaryIO):
    """Yield logical records. TextIOWrapper handles BOMs and quoted newlines."""
    wrapper = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="", errors="strict")
    csv.field_size_limit(settings().max_upload_bytes)
    reader = csv.reader(wrapper, delimiter=",", strict=True)
    try:
        headers = next(reader, None)
        if not headers or any(not h.strip() or "\x00" in h for h in headers):
            raise Problem(
                "invalid_headers", "A nonempty header row with no blank headers is required."
            )
        if len(headers) > settings().max_columns:
            raise Problem("column_limit", "The CSV exceeds the configured column limit.")
        if len({h.strip().casefold() for h in headers}) != len(headers):
            raise Problem(
                "duplicate_headers",
                "Headers must be unique, including after trimming and case folding.",
            )
        yield 0, headers
        for number, row in enumerate(reader, 1):
            if number > settings().max_rows:
                raise Problem("record_limit", "The CSV exceeds the configured data-record limit.")
            yield number, row
    except UnicodeDecodeError as exc:
        raise Problem(
            "unsupported_encoding", "Only UTF-8 and UTF-8 with BOM are supported."
        ) from exc
    except csv.Error as exc:
        raise Problem("malformed_csv", f"Malformed comma-separated CSV: {exc}") from exc
    finally:
        # The caller owns the underlying file/stream.
        wrapper.detach()


def inspect_file(stream: BinaryIO) -> tuple[list[str], list[list[str]]]:
    iterator = records(stream)
    try:
        _, headers = next(iterator)
        sample = []
        for _, row in iterator:
            sample.append(row)
            if len(sample) == 5:
                break
        return headers, sample
    finally:
        iterator.close()


def safe_csv_cell(value) -> str:
    text = "" if value is None else str(value)
    # Check past leading whitespace and controls; original data stays intact in PostgreSQL.
    if text.lstrip(" \t\r\n\v\f\x00").startswith(("=", "+", "-", "@")) or text.startswith(
        ("\t", "\r", "\n")
    ):
        return "'" + text
    return text
