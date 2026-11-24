"""业务路由汇总：领域专用路由先注册，通用资源路由兜底动态 /{module} 路径。"""
from __future__ import annotations

from app.routers import backfill_quality, resource

ROUTERS = [backfill_quality, resource]
