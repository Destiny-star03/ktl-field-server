"""현장 서버 API 라우터 패키지 (auth, cases, photos, events, admin).

계약 근거 (02-API명세.md §2.5):
- 휴대폰 API: /api/v1 (auth, categories, cases, photos, events)
- 관리 API: /api/admin/v1 (휴대폰 API의 /cases, /photos, /events를 동일 경로로 공유 + admin 전용 라우터)
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.categories import router as categories_router
from app.api.cases import router as cases_router
from app.api.events import router as events_router
from app.api.photos import router as photos_router

mobile_api_router = APIRouter(prefix="/api/v1")
mobile_api_router.include_router(auth_router)
mobile_api_router.include_router(categories_router)
mobile_api_router.include_router(cases_router)
mobile_api_router.include_router(photos_router)
mobile_api_router.include_router(events_router)

# 02-API명세.md §2.5: 휴대폰 API의 /cases, /photos, /events는 관리 API에서도 같은 경로로 사용 가능
admin_api_router = APIRouter(prefix="/api/admin/v1")
admin_api_router.include_router(cases_router)
admin_api_router.include_router(photos_router)
admin_api_router.include_router(events_router)
admin_api_router.include_router(admin_router)

__all__ = ["admin_api_router", "mobile_api_router"]
