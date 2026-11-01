"""분류 라우터 (§2.2 — 휴대폰 API `/categories`)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_settings
from app.api.schemas import Category, CategoryList
from app.models import ServerSettings
from app.modules import load_categories

router = APIRouter(tags=["cases"])


@router.get("/categories", response_model=CategoryList, summary="분류·촬영 항목·출력 칼럼 (categories.yaml)")
def list_categories(settings: ServerSettings = Depends(get_settings)) -> CategoryList:
    """categories.yaml을 JSON으로. 휴대폰 분류 선택에 안 나오는 분류(phone: false)는 뺀다 (02 §3.4)."""
    data = load_categories(settings.categories_path)
    return CategoryList(
        items=[
            Category(
                category=c["category"],
                label=c["label"],
                options=c.get("options") or [],
                items=c.get("items") or [],
                export=c.get("export") or [],
            )
            for c in data["categories"]
            if c.get("phone", False)
        ]
    )
