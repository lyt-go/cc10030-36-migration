"""轨迹质量领域服务：对采集批次定位点执行漂移检测、重复率和缺失率校验，生成质量报告数据集。

迁移后质检后端不再只采用旧累计值，而是关联回补服务已核验的终端 × 事件区间
及对应点位凭据；质量复验（证书）只能在范围被已核验区间覆盖时提交，
待复验时段随区间核验结果动态更新。只有累计数量依据的旧回执不能放行复验。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.interval_util import merge_intervals, uncovered_pieces
from app.store import store


class GeoQualityService(BaseModuleService):
    MODULE = "geo_quality"
    ENTITY = "质量报告"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["报告编号", "批次编号", "漂移点数", "重复点数", "缺失率", "异常时段", "校验规则", "质检结论"]
    REQUIRED_FIELDS = ["报告编号", "批次编号", "漂移点数"]
    STATUS_ORDER = ["草稿", "待质检", "质检中", "已出报告"]
    ACTION_RULES = {"启动质检": "待质检", "出具报告": "已出报告", "标记驳回": "已出报告"}
    NEGATIVE_ACTIONS = []

    def check_quality(self, batch_key: str, max_speed_mps: float = 50.0,
                       max_gap_sec: float = 300.0) -> dict[str, Any]:
        """读取采集批次定位点数据文件，执行漂移检测、重复率统计和缺失率判定，生成质量校验报告数据集。"""
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据文件不存在")

        points = self._read_jsonl(points_file)
        if not points:
            return {"batch_key": batch_key, "total_points": 0}

        total = len(points)
        # 时间排序
        points.sort(key=lambda p: p.get("timestamp", 0))

        drift_count = 0
        duplicate_count = 0
        gap_count = 0
        anomaly_periods = []

        for i in range(1, total):
            prev = points[i - 1]
            curr = points[i]
            time_diff = abs(curr.get("timestamp", 0) - prev.get("timestamp", 0))
            dist = self._haversine(
                prev.get("lat", 0), prev.get("lon", 0),
                curr.get("lat", 0), curr.get("lon", 0)
            )

            # 漂移检测：速度超过阈值
            if time_diff > 0:
                speed = dist / time_diff
                if speed > max_speed_mps:
                    drift_count += 1
                    anomaly_periods.append({
                        "point_id": curr.get("point_id"),
                        "speed_mps": round(speed, 2),
                        "time": time.strftime("%H:%M:%S", time.localtime(curr["timestamp"])),
                        "type": "drift",
                    })

            # 重复点检测：同一终端时间和距离相近
            if (prev.get("terminal") == curr.get("terminal") and
                time_diff < 5 and dist < 1.0):
                duplicate_count += 1

            # 缺失检测：时间间隔过大
            if time_diff > max_gap_sec:
                gap_count += 1
                anomaly_periods.append({
                    "time": time.strftime("%H:%M:%S", time.localtime(prev["timestamp"])),
                    "gap_sec": round(time_diff, 1),
                    "type": "gap",
                })

        missing_rate = round(gap_count / max(total, 1), 4)
        drift_rate = round(drift_count / max(total, 1), 4)
        dup_rate = round(duplicate_count / max(total, 1), 4)

        # 质检结论
        if drift_rate < 0.05 and missing_rate < 0.1:
            conclusion = "合格"
        elif drift_rate < 0.2 and missing_rate < 0.3:
            conclusion = "待复核"
        else:
            conclusion = "不合格"

        report = {
            "batch_key": batch_key,
            "total_points": total,
            "drift_points": drift_count,
            "duplicate_points": duplicate_count,
            "gap_points": gap_count,
            "drift_rate": drift_rate,
            "duplicate_rate": dup_rate,
            "missing_rate": missing_rate,
            "max_speed_mps": max_speed_mps,
            "max_gap_sec": max_gap_sec,
            "anomaly_periods": anomaly_periods[:10],  # 只保留前10个异常
            "conclusion": conclusion,
            "checked_at": time.time(),
        }

        # 关联已核验的回补区间与对应点，而不是只采用旧累计值
        backfill_coverage = self._backfill_service().get_verified_coverage(batch_key)
        report["backfill_coverage"] = {
            "verified_intervals": backfill_coverage["verified_intervals"],
            "unverified_intervals": backfill_coverage["unverified_intervals"],
            "count_receipts": backfill_coverage["count_receipts"],
        }

        output_file = self._data_dir("quality_reports") / f"{batch_key}_quality.jsonl"
        written = self._write_jsonl(output_file, [report])

        # 创建质检报告记录
        entry, _ = self.create_entry({
            "报告编号": f"QUAL-{batch_key}",
            "批次编号": batch_key,
            "漂移点数": str(drift_count),
            "重复点数": str(duplicate_count),
            "缺失率": f"{missing_rate:.1%}",
            "异常时段": f"间隙{gap_count}段/漂移{drift_count}点",
            "校验规则": f"最大速度{max_speed_mps}m/s,最大间隙{max_gap_sec}s",
            "质检结论": conclusion,
        })
        if entry:
            self.run_action(entry["id"], "出具报告")

        return {
            "batch_key": batch_key,
            "total_points": total,
            "drift_count": drift_count,
            "duplicate_count": duplicate_count,
            "gap_count": gap_count,
            "missing_rate": f"{missing_rate:.1%}",
            "conclusion": conclusion,
            "output_file": str(output_file),
        }

    def get_quality_report(self, batch_key: str) -> dict[str, Any]:
        """读取批次最新的轨迹质量报告数据集（报告按次追加，取最后一份）。"""
        file_path = self._data_dir("quality_reports") / f"{batch_key}_quality.jsonl"
        reports = self._read_jsonl(file_path)
        return reports[-1] if reports else {}

    # ------------------------------------------------------------------
    # 基于已核验回补区间的质量复验
    # ------------------------------------------------------------------

    def start_reverification(self, batch_key: str, scopes: list[dict[str, Any]],
                             report_id: str | None = None) -> dict[str, Any]:
        """开启一项质量复验，登记待复验的终端 × 事件区间范围。

        开始时冻结一份覆盖快照（复验开始时的覆盖），用于和确认时的联合覆盖
        对照；待复验时段按当前已核验区间实时计算。
        """
        backfill = self._backfill_service()
        normalized = [
            {
                "terminal": scope.get("terminal"),
                "start_date": scope["start_date"],
                "end_date": scope["end_date"],
            }
            for scope in scopes
        ]
        coverage_snapshot = backfill.get_verified_coverage(batch_key)
        state = {
            "reverification_id": f"REV-{batch_key}-{int(time.time())}",
            "batch_key": batch_key,
            "report_id": report_id or f"QUAL-{batch_key}",
            "status": "in_progress",
            "scopes": normalized,
            "started_at": time.time(),
            "coverage_at_start": {
                "verified_intervals": coverage_snapshot["verified_intervals"],
                "unverified_intervals": coverage_snapshot["unverified_intervals"],
            },
            "pending_intervals": self._pending_for_scopes(batch_key, normalized),
            "certificate": None,
        }
        self._save_reverification(batch_key, state)
        return state

    def get_reverification(self, batch_key: str) -> dict[str, Any]:
        """读取复验状态，并按最新核验结果刷新待复验时段。"""
        state = self._load_reverification(batch_key)
        if state is None:
            return {"batch_key": batch_key, "exists": False}
        if state.get("status") == "in_progress":
            state["pending_intervals"] = self._pending_for_scopes(
                batch_key, state["scopes"])
            self._save_reverification(batch_key, state)
        state["exists"] = True
        return state

    def submit_reverification(self, batch_key: str) -> dict[str, Any]:
        """提交复验并生成证书。

        只有全部范围在确认时都被已核验区间（真实点文件凭据）覆盖才允许提交；
        数量回执 / 旧累计值不计入覆盖。证书按确认时的联合覆盖生成，同时在
        ``start_coverage`` 中保留复验开始时的覆盖快照以备审计；本次仍未覆盖的
        时段留在 ``remaining_pending_intervals``（提交成功时为空）。
        """
        state = self._load_reverification(batch_key)
        if state is None:
            raise FileNotFoundError(f"批次 {batch_key} 没有进行中的质量复验")
        if state.get("status") != "in_progress":
            return {"ok": False, "reason": "复验已提交或已关闭", "state": state}

        backfill = self._backfill_service()
        coverage = backfill.get_verified_coverage(batch_key)
        covered = coverage["verified_intervals"]

        scope_results = []
        all_covered = True
        for scope in state["scopes"]:
            scope_coverage = backfill.coverage_for_scope(
                batch_key, scope["terminal"], scope["start_date"], scope["end_date"])
            scope_results.append(scope_coverage)
            if not scope_coverage["fully_covered"]:
                all_covered = False

        if not all_covered:
            state["pending_intervals"] = self._pending_for_scopes(
                batch_key, state["scopes"])
            state["last_blocked_at"] = time.time()
            self._save_reverification(batch_key, state)
            return {
                "ok": False,
                "reason": "存在未被已核验区间覆盖的时段，数量回执不能替代区间凭据",
                "scope_results": scope_results,
                "pending_intervals": state["pending_intervals"],
            }

        # 证书按确认时的联合覆盖生成
        confirming_intervals = self._matching_verified_intervals(batch_key, state["scopes"])
        certificate = {
            "certificate_id": f"CERT-{batch_key}-{int(time.time())}",
            "batch_key": batch_key,
            "issued_at": time.time(),
            "coverage_basis": "verified_interval_at_confirmation",
            "confirmed_coverage": merge_intervals([
                {"terminal": iv["terminal"], "start_date": iv["start_date"], "end_date": iv["end_date"]}
                for iv in confirming_intervals
            ]),
            "start_coverage": state["coverage_at_start"]["verified_intervals"],
            "scopes": state["scopes"],
            "interval_batches": [
                {"batch_id": iv["batch_id"], "checksum": iv.get("checksum")}
                for iv in confirming_intervals
            ],
            "remaining_pending_intervals": [],
        }
        state["status"] = "submitted"
        state["certificate"] = certificate
        state["pending_intervals"] = []
        state["submitted_at"] = time.time()
        self._save_reverification(batch_key, state)
        return {"ok": True, "certificate": certificate, "state": state}

    def _pending_for_scopes(self, batch_key: str,
                            scopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        backfill = self._backfill_service()
        coverage = backfill.get_verified_coverage(batch_key)
        covered = coverage["verified_intervals"]
        pending: list[dict[str, Any]] = []
        for scope in scopes:
            pending.extend(uncovered_pieces(
                covered, scope.get("terminal"), scope["start_date"], scope["end_date"]))
        return merge_intervals(pending)

    def _matching_verified_intervals(self, batch_key: str,
                                     scopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        coverage = self._backfill_service().get_verified_coverage(batch_key)
        matched = []
        for iv in coverage["verified_interval_batches"]:
            for scope in scopes:
                if iv.get("terminal") not in (scope.get("terminal"), "_ANY", None):
                    continue
                # 与该复验范围有交集的已核验区间批次都进入证书
                if iv["end_date"] >= scope["start_date"] and iv["start_date"] <= scope["end_date"]:
                    matched.append(iv)
                    break
        return matched

    def _backfill_service(self):
        from app.services.geo_backfill import GeoBackfillService
        return GeoBackfillService()

    def _reverification_file(self, batch_key: str):
        return self._data_dir("quality_reverifications") / f"{batch_key}.json"

    def _load_reverification(self, batch_key: str) -> dict[str, Any] | None:
        path = self._reverification_file(batch_key)
        if not path.exists():
            return None
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_reverification(self, batch_key: str, state: dict[str, Any]) -> None:
        import json
        path = self._reverification_file(batch_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, default=str)