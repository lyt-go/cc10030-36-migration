"""历史回补 / 轨迹质量迁移领域接口。

只承载迁移引入的新动作（区间证明构建、旧入口重试、旧回执登记、
质量复验生命周期）；通用 CRUD 仍走 resource 路由。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.geo_backfill import GeoBackfillService
from app.services.geo_quality import GeoQualityService

router = APIRouter(prefix="/api", tags=["回补区间与质量复验"])

backfill_service = GeoBackfillService()
quality_service = GeoQualityService()


class BatchPayload(BaseModel):
    batch_key: str


class BackfillPayload(BaseModel):
    batch_key: str
    late_point_count: int = 15
    start_date: str = "2026-09-01"
    end_date: str = "2026-09-07"
    terminal: str | None = None


class ReceiptPayload(BaseModel):
    batch_key: str
    total_after_backfill: int
    backfill_count: int | None = None
    start_date: str | None = None
    end_date: str | None = None


class Scope(BaseModel):
    terminal: str | None = None
    start_date: str
    end_date: str


class ReverificationPayload(BaseModel):
    batch_key: str
    scopes: list[Scope] = Field(default_factory=list)
    report_id: str | None = None


# -- 历史回补：区间证明与旧入口 ---------------------------------------

@router.post("/geo_backfill/run")
def run_backfill(payload: BackfillPayload) -> dict[str, Any]:
    return backfill_service.backfill_late_data(
        payload.batch_key,
        late_point_count=payload.late_point_count,
        start_date=payload.start_date,
        end_date=payload.end_date,
        terminal=payload.terminal,
    )


@router.post("/geo_backfill/verify")
def verify_backfill(payload: BatchPayload) -> dict[str, Any]:
    """新口径：按终端 × 事件区间独立核验回补批次。"""
    return backfill_service.verify_backfill_complete(payload.batch_key)


@router.post("/geo_backfill/build-interval-proofs")
def build_interval_proofs(payload: BatchPayload) -> dict[str, Any]:
    """新版上线时为有点文件的存量回补构建区间证明。"""
    return backfill_service.build_interval_proofs_for_existing(payload.batch_key)


@router.post("/geo_backfill/legacy-verify")
def legacy_verify(payload: BatchPayload) -> dict[str, Any]:
    """旧入口重试：从原有效证据恢复，再尝试切回新口径。"""
    return backfill_service.legacy_verify_complete(payload.batch_key)


@router.post("/geo_backfill/receipts")
def register_receipt(payload: ReceiptPayload) -> dict[str, Any]:
    """登记只有累计数量依据的旧完成回执（仅用于对账）。"""
    return backfill_service.register_completion_receipt(
        payload.batch_key,
        total_after_backfill=payload.total_after_backfill,
        backfill_count=payload.backfill_count,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )


@router.get("/geo_backfill/{batch_key}/coverage")
def backfill_coverage(batch_key: str) -> dict[str, Any]:
    return backfill_service.get_verified_coverage(batch_key)


@router.get("/geo_backfill/{batch_key}/coverage/{terminal}/{start_date}/{end_date}")
def scope_coverage(batch_key: str, terminal: str, start_date: str, end_date: str) -> dict[str, Any]:
    return backfill_service.coverage_for_scope(batch_key, terminal, start_date, end_date)


# -- 轨迹质量：基于已核验区间的复验 -----------------------------------

@router.post("/geo_quality/reverification/start")
def start_reverification(payload: ReverificationPayload) -> dict[str, Any]:
    scopes = [s.model_dump() for s in payload.scopes]
    return quality_service.start_reverification(
        payload.batch_key, scopes=scopes, report_id=payload.report_id)


@router.get("/geo_quality/reverification/{batch_key}")
def get_reverification(batch_key: str) -> dict[str, Any]:
    return quality_service.get_reverification(batch_key)


@router.post("/geo_quality/reverification/submit")
def submit_reverification(payload: BatchPayload) -> dict[str, Any]:
    return quality_service.submit_reverification(payload.batch_key)
