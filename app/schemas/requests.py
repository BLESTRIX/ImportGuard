import re
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def trim_metadata(cls, value, info):
        # Passwords are opaque, including their surrounding whitespace.
        if isinstance(value, str) and info.field_name != "password":
            return value.strip()
        return value


class Register(Input):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    organization_name: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class Login(Input):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ProductCreate(Input):
    sku: str = Field(max_length=100)
    name: str = Field(max_length=250)
    category: str | None = Field(default=None, max_length=150)
    unit_price: str = Field(max_length=40)
    stock_quantity: str | Annotated[int, Field(strict=True)]

    @field_validator("unit_price", mode="before")
    @classmethod
    def monetary_input(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Send monetary values as decimal strings to preserve precision")
        return str(value)


class ProductPatch(Input):
    version: int = Field(ge=1)
    sku: str | None = Field(default=None, max_length=100)
    name: str | None = Field(default=None, max_length=250)
    category: str | None = Field(default=None, max_length=150)
    unit_price: str | None = Field(default=None, max_length=40)
    stock_quantity: str | Annotated[int, Field(strict=True)] | None = None

    @field_validator("unit_price", mode="before")
    @classmethod
    def monetary_input(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Send monetary values as decimal strings")
        return None if value is None else str(value)


class MappingInput(Input):
    mapping: dict[str, str]


class TemplateCreate(MappingInput):
    name: str = Field(min_length=1, max_length=100)


class TemplatePatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    mapping: dict[str, str] | None = None


class OrganizationUpdate(Input):
    name: str = Field(min_length=1, max_length=200)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class ApplyInput(Input):
    confirmed: bool = False
    preview_revision: UUID


def valid_key(key: str):
    if not re.fullmatch(r"[\x21-\x7e]{1,200}", key):
        raise ValueError("Use 1–200 printable ASCII characters without spaces")
    return key
