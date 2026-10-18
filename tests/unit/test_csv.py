import io

import pytest

from app.core.errors import Problem
from app.importers.csv_importer import (
    inspect_file,
    normalize,
    records,
    safe_csv_cell,
    suggest_mapping,
    validate_mapping,
)


def values(**overrides):
    return {
        "sku": " abc-1 ",
        "name": " Tea ",
        "unit_price": "12.30",
        "stock_quantity": "4",
        **overrides,
    }


def test_normalization_and_category_semantics():
    row, errors = normalize(values())
    assert not errors
    assert row == {
        "sku": "ABC-1",
        "name": "Tea",
        "unit_price": "12.30",
        "stock_quantity": 4,
        "category": None,
    }
    assert "category" not in normalize(values(), category_mapped=False)[0]


@pytest.mark.parametrize(
    "price",
    ["NaN", "Infinity", "1e2", "$1", "1,000.00", "1.001", "-1", "+1", "10000000000", "", "١٢"],
)
def test_reject_unsafe_prices(price):
    assert any(e.field == "unit_price" for e in normalize(values(unit_price=price))[1])


@pytest.mark.parametrize("quantity", ["2.0", "1.5", "-1", "+2", "1e2", "1000000001", "", "NaN"])
def test_reject_non_integer_quantities(quantity):
    assert any(e.field == "stock_quantity" for e in normalize(values(stock_quantity=quantity))[1])


def test_decimal_boundaries_and_ascii_sku():
    assert not normalize(values(unit_price="9999999999.99", stock_quantity="1000000000"))[1]
    assert normalize(values(sku="ß"))[1]
    assert normalize(values(sku="a" * 65))[1]
    assert normalize(values(name="x" * 201))[1]


def test_bom_quotes_and_logical_records():
    data = '\ufeffsku,name,price,qty\r\na,"Tea, green",1.20,2\r\nb,"Two\nlines",0,3\r\n'.encode()
    parsed = list(records(io.BytesIO(data)))
    assert parsed[0] == (0, ["sku", "name", "price", "qty"])
    assert parsed[1][1][1] == "Tea, green"
    assert parsed[2] == (2, ["b", "Two\nlines", "0", "3"])


@pytest.mark.parametrize(
    "data,code",
    [
        (b"sku, SKU\na,b", "duplicate_headers"),
        (b"sku,\na,b", "invalid_headers"),
        (b"a\n\xff", "unsupported_encoding"),
        (b'a,b\n"unclosed,b', "malformed_csv"),
        (b"", "invalid_headers"),
    ],
)
def test_invalid_csv(data, code):
    with pytest.raises(Problem) as exc:
        list(records(io.BytesIO(data)))
    assert exc.value.code == code


def test_aliases_are_deterministic_and_ambiguous_columns_not_suggested():
    assert suggest_mapping(["product_code", "price", "qty", "name"])["sku"] == "product_code"
    assert "sku" not in suggest_mapping(["sku", "product_code"])
    with pytest.raises(Problem):
        validate_mapping({"sku": "a", "name": "a", "unit_price": "p", "stock_quantity": "q"})
    with pytest.raises(Problem):
        validate_mapping({"sku": "a", "name": "b", "unit_price": "p", "stock_quantity": "q"}, ["a"])


@pytest.mark.parametrize(
    "cell", ["=HYPERLINK(1)", "+1", "-1", "@SUM(A1)", "  =SUM(1)", "\twhatever", "\rvalue"]
)
def test_formula_escaping(cell):
    assert safe_csv_cell(cell) == "'" + cell
    assert safe_csv_cell("Normal") == "Normal"


def test_record_and_column_limits(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings(), "max_rows", 1)
    with pytest.raises(Problem, match="data-record"):
        list(records(io.BytesIO(b"a\n1\n2\n")))
    monkeypatch.setattr(settings(), "max_columns", 1)
    with pytest.raises(Problem, match="column limit"):
        inspect_file(io.BytesIO(b"a,b\n"))
