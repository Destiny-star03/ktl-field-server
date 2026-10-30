"""요청·응답 스키마 — field-server.openapi.yaml `components/schemas`와 같은 모양."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CaseStatus = Literal["open", "completed"]
ItemStatus = Literal["empty", "queued", "reading", "done", "needs_check", "confirmed", "failed"]


class CategoryOption(BaseModel):
    key: str
    label: str
    choices: list[str]


class CategoryItem(BaseModel):
    key: str = Field(examples=["prec_1"])
    label: str = Field(examples=["정밀오차 1"])
    reader: str = Field(examples=["flowmeter"], description="none이면 판독 없이 사진만")
    required: bool


class Category(BaseModel):
    category: str = Field(examples=["sampling"])
    label: str = Field(examples=["시료채취"])
    options: list[CategoryOption]
    items: list[CategoryItem]
    export: list[str]


class CategoryList(BaseModel):
    items: list[Category]


class CaseCreate(BaseModel):
    receipt_no: str = Field(pattern=r"^\d{2}-\d{6}-\d{2}$", examples=["26-000123-01"])
    category: str = Field(examples=["sampling"])
    unit_count: int = Field(ge=1, le=50, examples=[2])
    options: dict[str, str] = Field(default_factory=dict, examples=[{"flowmeter_type": "defender"}])


class Progress(BaseModel):
    total: int
    confirmed: int
    needs_check: int


class CaseSummary(BaseModel):
    id: str
    receipt_no: str
    category: str
    unit_count: int
    status: CaseStatus
    progress: Progress
    updated_at: str = Field(json_schema_extra={"format": "date-time"})


class UnitItem(BaseModel):
    key: str
    label: str
    required: bool
    photo_id: str | None
    status: ItemStatus


class Unit(BaseModel):
    index: int
    items: list[UnitItem]


class Case(CaseSummary):
    options: dict[str, str]
    created_by: str
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    units: list[Unit]


class CaseList(BaseModel):
    items: list[CaseSummary]
    next_cursor: str | None


PhotoStatus = Literal["queued", "reading", "done", "failed", "deleted"]
ValueStatus = Literal["auto", "needs_check", "confirmed", "edited"]


class Value(BaseModel):
    key: str = Field(examples=["flow"])
    label: str = Field(examples=["유량"])
    value: str = Field(examples=["1.002"])
    unit: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    bbox: list[int] | None = Field(default=None, min_length=4, max_length=4)
    status: ValueStatus
    confirmed_by: str | None = None
    confirmed_at: str | None = Field(default=None, json_schema_extra={"format": "date-time"})


class PhotoWarning(BaseModel):
    model_config = ConfigDict(title="Warning")  # 계약 스키마 이름 그대로, 파이썬 내장 Warning은 가리지 않게

    code: str = Field(examples=["PRE_MEASURE_SCREEN"])
    message: str


class Photo(BaseModel):
    id: str = Field(examples=["ph_301"])
    case_id: str
    unit_index: int
    item_key: str
    reader: str
    status: PhotoStatus
    sha256: str
    stored_name: str = Field(examples=["20261029_091102_prec_1.jpg"])
    uploaded_by: str
    uploaded_at: str = Field(json_schema_extra={"format": "date-time"})
    values: list[Value]
    warnings: list[PhotoWarning]
    error: str | None
    read_ms: int | None
