"""历史回补领域服务。

完成口径迁移（按点核对 → 按终端 × 事件区间核对）：

- 旧检查点 ``checkpoints/backfill_{batch}.json`` 只记录累计点数，点数相同的两次
  补录可能覆盖不同缺口，不能作为完成依据，仅保留用于进度对账。
- 新证据是区间证明 ``backfill_proofs/{batch}.json``：按终端 × 事件日期区间登记
  回补批次，每个区间附真实点文件中的点位凭据与校验和，并经
  :meth:`verify_backfill_complete` 独立核验后才标记为 ``verified``。
- 只有累计数量依据、没有点文件/区间凭据的旧完成回执落在
  ``backfill_receipts/{batch}.json``，只能支持进度对账，不能让缺证区间跳过复验。
- ``legacy_verify_complete`` 是旧入口：部署失败时可从原有效证据（旧检查点）
  恢复；只要点文件仍在，就先补建区间证明再切回新口径。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.interval_util import is_covered, merge_intervals, uncovered_pieces

# 事件日期落在哪一天：优先用点上显式写入的 event_date，其次用 timestamp 推导。
_DATE_FMT = "%Y-%m-%d"


class GeoBackfillService(BaseModuleService):
    MODULE = "geo_backfill"
    ENTITY = "回补任务"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["回补编号", "目标数据集", "起始日期", "截止日期", "回补范围", "并行度", "覆盖方式", "回补水位"]
    REQUIRED_FIELDS = ["回补编号", "目标数据集", "起始日期"]
    STATUS_ORDER = ["草稿", "待回补", "回补中", "已完成", "已取消"]
    ACTION_RULES = {"启动回补": "回补中", "标记完成": "已完成", "取消回补": "已取消"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 回补执行
    # ------------------------------------------------------------------

    def backfill_late_data(self, batch_key: str, late_point_count: int = 15,
                           start_date: str = "2026-09-01",
                           end_date: str = "2026-09-07",
                           terminal: str | None = None) -> dict[str, Any]:
        """为目标采集批次生成迟到数据并追加回补到定位点数据集文件。

        迁移后同时产出两类证据：
        1. 旧累计检查点（保留，供旧入口恢复与进度对账）；
        2. 新区间证明，并立即对新批次执行一次独立核验。
        """
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据文件不存在")

        existing = self._read_jsonl(points_file)
        existing_count = len(existing)

        # 生成迟到数据，补齐终端与事件日期，保证区间可按天核对
        late_points = self._generate_sample_points(count=late_point_count)
        eff_terminal = terminal or self._guess_terminal(existing) or f"TERM-BF-{batch_key}"
        for p in late_points:
            p["batch_key"] = batch_key
            p["is_backfill"] = True
            p["terminal"] = eff_terminal
            p["backfill_date_range"] = f"{start_date}~{end_date}"
            p.setdefault("event_date", self._point_event_date(p))

        written = self._write_jsonl(points_file, late_points)

        # 旧检查点：累计口径，迁移后仅用于对账与旧入口恢复
        self._write_checkpoint(f"backfill_{batch_key}", {
            "batch_key": batch_key,
            "existing_count": existing_count,
            "backfill_count": written,
            "total_after_backfill": existing_count + written,
            "start_date": start_date,
            "end_date": end_date,
            "backfilled_at": time.time(),
        })

        # 新区间证明：按终端 × 事件区间登记本批次
        batch_id = f"BF-{batch_key}-{int(time.time())}"
        proof = self._load_proof(batch_key)
        proof["batch_key"] = batch_key
        proof["batches"].append({
            "batch_id": batch_id,
            "terminal": eff_terminal,
            "start_date": start_date,
            "end_date": end_date,
            "declared_points": written,
            "source": "backfill_late_data",
            "created_at": time.time(),
            "verified": False,
            "evidence": [],
        })
        self._save_proof(batch_key, proof)
        verify_result = self.verify_backfill_complete(batch_key)

        entry, _ = self.create_entry({
            "回补编号": f"BF-{batch_key}",
            "目标数据集": batch_key,
            "起始日期": start_date,
            "截止日期": end_date,
            "回补范围": f"{start_date}~{end_date}",
            "并行度": "1",
            "覆盖方式": "append",
            "回补水位": f"已回补{written}/{existing_count+written}条",
        })
        if entry:
            self.run_action(entry["id"], "标记完成")

        return {
            "batch_key": batch_key,
            "existing_points": existing_count,
            "backfill_points": written,
            "total_after": existing_count + written,
            "date_range": f"{start_date}~{end_date}",
            "terminal": eff_terminal,
            "interval_batch_id": batch_id,
            "verified_intervals": verify_result["verified_intervals"],
        }

    # ------------------------------------------------------------------
    # 新口径：按终端 × 事件区间独立核验回补批次
    # ------------------------------------------------------------------

    def verify_backfill_complete(self, batch_key: str) -> dict[str, Any]:
        """按终端与事件区间核验回补批次，而不是按累计点数核对。

        核验完全基于点文件中的真实点位（区间凭据），不读取旧检查点的累计数：
        区间内每个事件日都能在点文件中找到对应终端的回补点，区间才算
        ``verified``；否则保留为未核验并给出未覆盖事件日。
        """
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        points = self._read_jsonl(points_file) if points_file.exists() else []

        # 点文件实际覆盖：终端 -> 事件日期集合
        coverage: dict[str, set[str]] = {}
        point_refs: dict[tuple[str, str], list[str]] = {}
        for p in points:
            if not p.get("is_backfill"):
                continue
            term = p.get("terminal") or "_ANY"
            day = self._point_event_date(p)
            coverage.setdefault(term, set()).add(day)
            point_refs.setdefault((term, day), []).append(str(p.get("point_id")))

        proof = self._load_proof(batch_key)
        verified_intervals: list[dict[str, Any]] = []
        unverified_intervals: list[dict[str, Any]] = []

        for batch in proof["batches"]:
            term = batch.get("terminal") or "_ANY"
            days_covered = coverage.get(term, set())
            missing_days = [
                day for day in self._enumerate_days(batch["start_date"], batch["end_date"])
                if day not in days_covered
            ]
            evidence = []
            for day in self._enumerate_days(batch["start_date"], batch["end_date"]):
                for pid in point_refs.get((term, day), [])[:3]:
                    evidence.append({"event_date": day, "terminal": term, "point_id": pid})

            if missing_days:
                batch["verified"] = False
                batch["missing_event_dates"] = missing_days
                batch["evidence"] = evidence
                unverified_intervals.append({
                    "batch_id": batch["batch_id"],
                    "terminal": term,
                    "start_date": batch["start_date"],
                    "end_date": batch["end_date"],
                    "missing_event_dates": missing_days,
                })
            else:
                batch["verified"] = True
                batch["missing_event_dates"] = []
                batch["evidence"] = evidence
                batch["verified_at"] = time.time()
                batch["checksum"] = self._compute_checksum(
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True))
                verified_intervals.append({
                    "batch_id": batch["batch_id"],
                    "terminal": term,
                    "start_date": batch["start_date"],
                    "end_date": batch["end_date"],
                    "point_evidence_count": len(evidence),
                    "checksum": batch["checksum"],
                })

        proof["verified_intervals"] = verified_intervals
        proof["unverified_intervals"] = unverified_intervals
        proof["last_verified_at"] = time.time()
        self._save_proof(batch_key, proof)

        receipt = self._load_receipt(batch_key)
        return {
            "batch_key": batch_key,
            "basis": "terminal_event_interval",
            "points_file_present": points_file.exists(),
            "interval_batches": len(proof["batches"]),
            "verified_intervals": verified_intervals,
            "unverified_intervals": unverified_intervals,
            "is_complete": len(unverified_intervals) == 0 and len(proof["batches"]) > 0,
            "receipts": receipt["receipts"],
        }

    def build_interval_proofs_for_existing(self, batch_key: str) -> dict[str, Any]:
        """新版上线时为有点文件的存量回补构建区间证明。

        从点文件中的回补点（``is_backfill``）按终端 × 事件日期推导实际覆盖，
        每个终端生成一个区间批次并立即独立核验。没有点文件时无法构建，返回
        ``built=False``，存量记录不会被误判成已填齐。
        """
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            return {"batch_key": batch_key, "built": False,
                    "reason": "点文件不存在，无法构建区间证明", "verified_intervals": []}

        points = self._read_jsonl(points_file)
        backfill_points = [p for p in points if p.get("is_backfill")]
        if not backfill_points:
            return {"batch_key": batch_key, "built": False,
                    "reason": "点文件中没有回补点，无法推导回补区间", "verified_intervals": []}

        term_days: dict[str, list[str]] = {}
        for p in backfill_points:
            term = p.get("terminal") or "_ANY"
            term_days.setdefault(term, []).append(self._point_event_date(p))

        proof = self._load_proof(batch_key)
        proof["batch_key"] = batch_key
        # 存量重建批次始终以点文件为准：先移除旧的迁移批次，再按当前真实点
        # 重新推导。这样确认前补齐的真实点文件能扩展已核验覆盖；
        # backfill_late_data 直接登记的批次（其它 source）保持不动。
        proof["batches"] = [
            b for b in proof["batches"]
            if b.get("source") != "migration_built_from_points_file"
        ]
        for term, days in term_days.items():
            for interval in self._days_to_intervals(sorted(set(days))):
                proof["batches"].append({
                    "batch_id": f"BF-LEGACY-{batch_key}-{term}-{interval['start_date']}",
                    "terminal": term,
                    "start_date": interval["start_date"],
                    "end_date": interval["end_date"],
                    "declared_points": sum(1 for p in backfill_points
                                           if (p.get("terminal") or "_ANY") == term
                                           and interval["start_date"] <= self._point_event_date(p) <= interval["end_date"]),
                    "source": "migration_built_from_points_file",
                    "created_at": time.time(),
                    "verified": False,
                    "evidence": [],
                })
        self._save_proof(batch_key, proof)

        verify_result = self.verify_backfill_complete(batch_key)
        return {
            "batch_key": batch_key,
            "built": True,
            "source": "points_file",
            "interval_batches": verify_result["interval_batches"],
            "verified_intervals": verify_result["verified_intervals"],
            "unverified_intervals": verify_result["unverified_intervals"],
        }

    # ------------------------------------------------------------------
    # 旧入口与旧完成回执
    # ------------------------------------------------------------------

    def legacy_verify_complete(self, batch_key: str) -> dict[str, Any]:
        """旧入口重试：从原有效证据恢复，再尝试切到新口径。

        - 点文件仍在：先为存量回补构建区间证明并独立核验，返回新口径结果；
        - 点文件已丢失：只能回退到旧检查点的累计数核对，结果显式标记
          ``evidence_basis=count_checkpoint``，不得用于质量复验放行。
        """
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        cp = self._read_checkpoint(f"backfill_{batch_key}")

        if points_file.exists():
            built = self.build_interval_proofs_for_existing(batch_key)
            if built.get("built"):
                verify_result = self.verify_backfill_complete(batch_key)
                verify_result["legacy_retry"] = True
                verify_result["recovered_from"] = "points_file"
                verify_result["count_checkpoint"] = cp
                return verify_result

        if cp is None:
            return {"batch_key": batch_key, "legacy_retry": True,
                    "evidence_basis": "none", "is_complete": False,
                    "verified_intervals": [], "unverified_intervals": []}

        current_count = len(self._read_jsonl(points_file)) if points_file.exists() else 0
        count_match = current_count == cp.get("total_after_backfill", 0)
        return {
            "batch_key": batch_key,
            "legacy_retry": True,
            "evidence_basis": "count_checkpoint",
            "points_file_present": points_file.exists(),
            "current_total": current_count,
            "total_after_backfill": cp.get("total_after_backfill", 0),
            "count_match": count_match,
            # 旧累计证据只能支持进度对账，不能等同区间核验完成
            "is_complete": False,
            "verified_intervals": [],
            "unverified_intervals": [],
            "note": "仅有累计数量依据，需补录真实点文件后再做区间核验",
        }

    def register_completion_receipt(self, batch_key: str, total_after_backfill: int,
                                    backfill_count: int | None = None,
                                    start_date: str | None = None,
                                    end_date: str | None = None) -> dict[str, Any]:
        """登记一条（可能迟到的）旧完成回执。

        旧回执只携带累计数量，写入 receipts 供进度对账；若该批次已存在可核验
        区间证明，则顺带重新核验区间。回执本身不会创建任何区间凭据。
        """
        receipt_book = self._load_receipt(batch_key)
        receipt = {
            "receipt_id": f"RCP-{batch_key}-{len(receipt_book['receipts']) + 1}",
            "batch_key": batch_key,
            "total_after_backfill": total_after_backfill,
            "backfill_count": backfill_count,
            "start_date": start_date,
            "end_date": end_date,
            "received_at": time.time(),
            "evidence_basis": "count_only",
        }
        receipt_book["batch_key"] = batch_key
        receipt_book["receipts"].append(receipt)
        self._save_receipt(batch_key, receipt_book)

        proof = self._load_proof(batch_key)
        verify_result = self.verify_backfill_complete(batch_key) if proof["batches"] else {
            "batch_key": batch_key, "is_complete": False,
            "verified_intervals": [], "unverified_intervals": [],
        }
        return {
            "registered": True,
            "receipt": receipt,
            "reconciliation_only": True,
            "interval_verified": verify_result.get("is_complete", False),
            "verified_intervals": verify_result.get("verified_intervals", []),
            "note": "数量回执仅用于对账，不产生区间凭据",
        }

    # ------------------------------------------------------------------
    # 供轨迹质量模块消费的覆盖查询
    # ------------------------------------------------------------------

    def get_verified_coverage(self, batch_key: str) -> dict[str, Any]:
        """返回批次当前的已核验/未核验区间覆盖与数量对账信息。"""
        proof = self._load_proof(batch_key)
        receipt_book = self._load_receipt(batch_key)
        cp = self._read_checkpoint(f"backfill_{batch_key}")

        verified = [
            {"terminal": b.get("terminal"), "start_date": b["start_date"], "end_date": b["end_date"],
             "batch_id": b["batch_id"], "checksum": b.get("checksum")}
            for b in proof["batches"] if b.get("verified")
        ]
        unverified = [
            {"terminal": b.get("terminal"), "start_date": b["start_date"], "end_date": b["end_date"],
             "batch_id": b["batch_id"], "missing_event_dates": b.get("missing_event_dates", [])}
            for b in proof["batches"] if not b.get("verified")
        ]
        return {
            "batch_key": batch_key,
            "verified_intervals": merge_intervals(
                [{"terminal": v["terminal"], "start_date": v["start_date"], "end_date": v["end_date"]}
                 for v in verified]),
            "verified_interval_batches": verified,
            "unverified_intervals": unverified,
            "count_checkpoint": cp,
            "count_receipts": receipt_book["receipts"],
        }

    def coverage_for_scope(self, batch_key: str, terminal: str | None,
                           start_date: str, end_date: str) -> dict[str, Any]:
        """查询某个复验范围当前被已核验区间覆盖到哪一步、还缺哪些片段。"""
        coverage = self.get_verified_coverage(batch_key)
        covered = coverage["verified_intervals"]
        fully_covered = is_covered(covered, terminal, start_date, end_date)
        pending = uncovered_pieces(covered, terminal, start_date, end_date)
        return {
            "batch_key": batch_key,
            "terminal": terminal,
            "scope": {"start_date": start_date, "end_date": end_date},
            "fully_covered": fully_covered,
            "pending_intervals": pending,
        }

    # ------------------------------------------------------------------
    # 证明/回执文件与辅助方法
    # ------------------------------------------------------------------

    def _proof_file(self, batch_key: str):
        return self._data_dir("backfill_proofs") / f"{batch_key}.json"

    def _receipt_file(self, batch_key: str):
        return self._data_dir("backfill_receipts") / f"{batch_key}.json"

    def _load_proof(self, batch_key: str) -> dict[str, Any]:
        path = self._proof_file(batch_key)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"batch_key": batch_key, "batches": [],
                "verified_intervals": [], "unverified_intervals": []}

    def _save_proof(self, batch_key: str, proof: dict[str, Any]) -> None:
        path = self._proof_file(batch_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(proof, f, ensure_ascii=False, indent=2, default=str)

    def _load_receipt(self, batch_key: str) -> dict[str, Any]:
        path = self._receipt_file(batch_key)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"batch_key": batch_key, "receipts": []}

    def _save_receipt(self, batch_key: str, book: dict[str, Any]) -> None:
        path = self._receipt_file(batch_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(book, f, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _enumerate_days(start_date: str, end_date: str) -> list[str]:
        import datetime
        start = datetime.date.fromisoformat(start_date)
        end = datetime.date.fromisoformat(end_date)
        days = []
        cur = start
        while cur <= end:
            days.append(cur.isoformat())
            cur += datetime.timedelta(days=1)
        return days

    @staticmethod
    def _days_to_intervals(days: list[str]) -> list[dict[str, str]]:
        """把连续日期列表压缩成若干闭区间。"""
        import datetime
        if not days:
            return []
        intervals = []
        start = prev = datetime.date.fromisoformat(days[0])
        for day_str in days[1:]:
            day = datetime.date.fromisoformat(day_str)
            if (day - prev).days == 1:
                prev = day
                continue
            intervals.append({"start_date": start.isoformat(), "end_date": prev.isoformat()})
            start = prev = day
        intervals.append({"start_date": start.isoformat(), "end_date": prev.isoformat()})
        return intervals

    @staticmethod
    def _point_event_date(point: dict[str, Any]) -> str:
        if point.get("event_date"):
            return str(point["event_date"])
        ts = point.get("timestamp")
        if ts:
            return time.strftime(_DATE_FMT, time.localtime(float(ts)))
        return time.strftime(_DATE_FMT, time.localtime())

    @staticmethod
    def _guess_terminal(points: list[dict[str, Any]]) -> str | None:
        for p in points:
            if p.get("terminal"):
                return str(p["terminal"])
        return None
