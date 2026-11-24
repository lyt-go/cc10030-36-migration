"""回补完成口径迁移测试：按点数核对 → 按终端 × 事件区间核对。

覆盖需求：
1. 新版上线先为有点文件的存量回补构建区间证明；无点文件存量记录不被误判填齐；
2. 旧检查点只记累计数，点数相同的两次补录覆盖不同缺口时结论不同；
3. 旧入口重试可从原有效证据恢复，再切回新口径；
4. 只有累计数量依据的旧回执只能对账，不能让缺证区间跳成复验完成；
5. 质量复验关联已核验回补区间和对应点，待复验时段随核验结果更新；
6. 证书按确认时的联合覆盖生成，并保留复验开始时的覆盖快照。
"""
from __future__ import annotations

import datetime
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_migrate_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["geo_points", "quality_reports", "checkpoints",
                "backfill_proofs", "backfill_receipts", "quality_reverifications"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    shutil.rmtree(tmpdir, ignore_errors=True)


def _onboard(geo, batch_key: str, terminal: str = "TERM-A", count: int = 10):
    return geo.onboard_batch({
        "批次编号": batch_key,
        "采集终端": terminal,
        "定位方式": "GPS",
        "采样频率": "1Hz",
    }, point_count=count)


def _append_backfill_points(service, batch_key: str, terminal: str,
                            event_dates: list[str]) -> None:
    """直接向点文件补录带显式事件日期的真实回补点（模拟迟到数据落盘）。"""
    points_file = service._data_dir("geo_points") / f"{batch_key}.jsonl"
    existing = service._read_jsonl(points_file)
    base_id = max((int(p.get("point_id", 0)) for p in existing), default=0)
    points = []
    for i, day in enumerate(event_dates, start=1):
        ts = datetime.datetime.fromisoformat(day).replace(hour=12).timestamp()
        points.append({
            "point_id": base_id + i,
            "lat": 39.9042, "lon": 116.4074,
            "timestamp": ts,
            "event_date": day,
            "terminal": terminal,
            "batch_key": batch_key,
            "is_backfill": True,
        })
    service._write_jsonl(points_file, points)


class TestIntervalProofMigration:
    def test_build_proofs_for_existing_backfill_with_points_file(self, setup_data_dir):
        """新版上线：有点文件的存量回补可构建区间证明并通过独立核验。"""
        from app.services.geo_source import GeoSourceService
        from app.services.geo_backfill import GeoBackfillService
        from app.services.geo_quality import GeoQualityService

        geo, backfill, quality = GeoSourceService(), GeoBackfillService(), GeoQualityService()
        batch_key = "MIG-001"
        _onboard(geo, batch_key)
        _append_backfill_points(
            backfill, batch_key, "TERM-A",
            ["2026-09-01", "2026-09-01", "2026-09-02", "2026-09-03"])

        # 迁移前：没有任何区间凭据，覆盖查询为空，不能算已核验
        coverage_before = backfill.get_verified_coverage(batch_key)
        assert coverage_before["verified_intervals"] == []

        result = backfill.build_interval_proofs_for_existing(batch_key)
        assert result["built"] is True
        assert result["source"] == "points_file"
        verified = result["verified_intervals"]
        assert len(verified) == 1
        assert verified[0]["terminal"] == "TERM-A"
        assert verified[0]["start_date"] == "2026-09-01"
        assert verified[0]["end_date"] == "2026-09-03"

        verify = backfill.verify_backfill_complete(batch_key)
        assert verify["basis"] == "terminal_event_interval"
        assert verify["is_complete"] is True
        assert verify["unverified_intervals"] == []

        # 质检后端关联的是已核验区间，而不是旧累计值
        quality.check_quality(batch_key)
        report = quality.get_quality_report(batch_key)
        assert report["backfill_coverage"]["verified_intervals"][0]["end_date"] == "2026-09-03"

    def test_existing_without_points_file_not_misjudged_filled(self, setup_data_dir):
        """只有累计值、没有点文件的存量记录不能被误判成已填齐。"""
        from app.services.geo_backfill import GeoBackfillService

        backfill = GeoBackfillService()
        batch_key = "MIG-NOFILE"
        backfill._write_checkpoint(f"backfill_{batch_key}", {
            "batch_key": batch_key, "existing_count": 10, "backfill_count": 5,
            "total_after_backfill": 15,
            "start_date": "2026-09-01", "end_date": "2026-09-07",
        })

        built = backfill.build_interval_proofs_for_existing(batch_key)
        assert built["built"] is False

        legacy = backfill.legacy_verify_complete(batch_key)
        assert legacy["evidence_basis"] == "count_checkpoint"
        assert legacy["is_complete"] is False
        assert legacy["verified_intervals"] == []

    def test_same_point_count_different_gaps_give_different_coverage(self, setup_data_dir):
        """两份都补到 5 点的回补，覆盖的缺口不同，区间核验结论必须不同。"""
        from app.services.geo_source import GeoSourceService
        from app.services.geo_backfill import GeoBackfillService

        geo, backfill = GeoSourceService(), GeoBackfillService()
        b1, b2 = "MIG-SAME-1", "MIG-SAME-2"
        _onboard(geo, b1)
        _onboard(geo, b2)
        # 第一份：5 个点覆盖 09-01~09-05 每天一个点
        _append_backfill_points(
            backfill, b1, "TERM-A",
            ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"])
        # 第二份：同样 5 个点，但全挤在 09-01
        _append_backfill_points(backfill, b2, "TERM-A", ["2026-09-01"] * 5)

        r1 = backfill.build_interval_proofs_for_existing(b1)
        r2 = backfill.build_interval_proofs_for_existing(b2)
        assert r1["verified_intervals"][0]["start_date"] == "2026-09-01"
        assert r1["verified_intervals"][0]["end_date"] == "2026-09-05"
        assert r2["verified_intervals"][0]["start_date"] == "2026-09-01"
        assert r2["verified_intervals"][0]["end_date"] == "2026-09-01"

        scope1 = backfill.coverage_for_scope(b1, "TERM-A", "2026-09-01", "2026-09-05")
        scope2 = backfill.coverage_for_scope(b2, "TERM-A", "2026-09-01", "2026-09-05")
        assert scope1["fully_covered"] is True
        assert scope2["fully_covered"] is False
        assert scope2["pending_intervals"] == [
            {"terminal": "TERM-A", "start_date": "2026-09-02", "end_date": "2026-09-05"}]

    def test_legacy_retry_recovers_then_switches_to_interval_basis(self, setup_data_dir):
        """先执行一次老入口重试：点文件还在时恢复并切到新区间口径。"""
        from app.services.geo_source import GeoSourceService
        from app.services.geo_backfill import GeoBackfillService

        geo, backfill = GeoSourceService(), GeoBackfillService()
        batch_key = "MIG-LEGACY"
        _onboard(geo, batch_key)
        _append_backfill_points(
            backfill, batch_key, "TERM-A", ["2026-09-01", "2026-09-02"])
        # 模拟迁移前留下的旧检查点（只记累计数）
        backfill._write_checkpoint(f"backfill_{batch_key}", {
            "batch_key": batch_key, "existing_count": 10, "backfill_count": 2,
            "total_after_backfill": 12,
        })

        result = backfill.legacy_verify_complete(batch_key)
        assert result["legacy_retry"] is True
        assert result["recovered_from"] == "points_file"
        assert result["basis"] == "terminal_event_interval"
        assert result["is_complete"] is True
        assert len(result["verified_intervals"]) == 1
        # 旧检查点仍被保留用于对账
        assert result["count_checkpoint"]["total_after_backfill"] == 12

    def test_backfill_late_data_writes_proof_and_verifies_interval(self, setup_data_dir):
        """新回补执行后直接产出区间证明并完成独立核验，同时保留旧检查点。"""
        from app.services.geo_source import GeoSourceService
        from app.services.geo_backfill import GeoBackfillService

        geo, backfill = GeoSourceService(), GeoBackfillService()
        batch_key = "MIG-RUN"
        _onboard(geo, batch_key)
        today = datetime.date.today().isoformat()

        result = backfill.backfill_late_data(
            batch_key, late_point_count=5, start_date=today, end_date=today,
            terminal="TERM-A")
        assert result["backfill_points"] == 5
        assert len(result["verified_intervals"]) == 1
        assert result["verified_intervals"][0]["terminal"] == "TERM-A"

        # 旧检查点仍在，供对账
        cp = backfill._read_checkpoint(f"backfill_{batch_key}")
        assert cp["total_after_backfill"] == 15


class TestCountOnlyReceipts:
    def test_count_receipt_is_reconciliation_only(self, setup_data_dir):
        """只有累计数量依据的回执只能支持进度对账，不产生区间凭据。"""
        from app.services.geo_source import GeoSourceService
        from app.services.geo_backfill import GeoBackfillService

        geo, backfill = GeoSourceService(), GeoBackfillService()
        batch_key = "MIG-RCP"
        _onboard(geo, batch_key)

        receipt = backfill.register_completion_receipt(
            batch_key, total_after_backfill=20, backfill_count=10,
            start_date="2026-09-01", end_date="2026-09-07")
        assert receipt["reconciliation_only"] is True
        assert receipt["interval_verified"] is False

        coverage = backfill.get_verified_coverage(batch_key)
        assert coverage["verified_intervals"] == []
        assert len(coverage["count_receipts"]) == 1
        assert coverage["count_receipts"][0]["evidence_basis"] == "count_only"

    def test_late_receipt_mid_migration_does_not_release_unverified_interval(self, setup_data_dir):
        """迁移中途到达的迟到旧完成回执，不能让缺证区间跳成复验完成。"""
        from app.services.geo_source import GeoSourceService
        from app.services.geo_backfill import GeoBackfillService
        from app.services.geo_quality import GeoQualityService

        geo, backfill, quality = (
            GeoSourceService(), GeoBackfillService(), GeoQualityService())
        batch_key = "MIG-LATE"
        _onboard(geo, batch_key)
        # 第一份区间有点文件、可核验：TERM-A 09-01~09-03
        _append_backfill_points(
            backfill, batch_key, "TERM-A",
            ["2026-09-01", "2026-09-02", "2026-09-03"])
        backfill.build_interval_proofs_for_existing(batch_key)

        # 一项仍未提交的质检复验，含两个范围
        state = quality.start_reverification(batch_key, scopes=[
            {"terminal": "TERM-A", "start_date": "2026-09-01", "end_date": "2026-09-03"},
            {"terminal": "TERM-A", "start_date": "2026-09-04", "end_date": "2026-09-07"},
        ])
        assert state["status"] == "in_progress"
        # 开始时只覆盖第一份区间
        assert state["coverage_at_start"]["verified_intervals"][0]["end_date"] == "2026-09-03"
        pending = quality.get_reverification(batch_key)["pending_intervals"]
        assert pending == [
            {"terminal": "TERM-A", "start_date": "2026-09-04", "end_date": "2026-09-07"}]

        # 迁移中途重放一条只有累计值的旧完成通知，声称整段 09-01~09-07 已完成
        backfill.register_completion_receipt(
            batch_key, total_after_backfill=99, backfill_count=7,
            start_date="2026-09-01", end_date="2026-09-07")

        blocked = quality.submit_reverification(batch_key)
        assert blocked["ok"] is False
        assert "区间凭据" in blocked["reason"]
        assert blocked["pending_intervals"] == [
            {"terminal": "TERM-A", "start_date": "2026-09-04", "end_date": "2026-09-07"}]
        # 复验仍停留在进行中，没有证书
        assert quality.get_reverification(batch_key)["certificate"] is None

    def test_reverification_completes_after_real_points_independently_verified(self, setup_data_dir):
        """第二份区间补齐真实点文件并通过迁移后独立核验后，复验才能提交。

        证书按确认时的联合覆盖生成，开始时覆盖仅第一份区间；
        提交后不再有待复验时段。
        """
        from app.services.geo_source import GeoSourceService
        from app.services.geo_backfill import GeoBackfillService
        from app.services.geo_quality import GeoQualityService

        geo, backfill, quality = (
            GeoSourceService(), GeoBackfillService(), GeoQualityService())
        batch_key = "MIG-TWO"
        _onboard(geo, batch_key)
        _append_backfill_points(
            backfill, batch_key, "TERM-A",
            ["2026-09-01", "2026-09-02", "2026-09-03"])
        backfill.build_interval_proofs_for_existing(batch_key)

        # 两个范围之间留一天（09-04）缺口，保证两段区间不会被合并成一条
        quality.start_reverification(batch_key, scopes=[
            {"terminal": "TERM-A", "start_date": "2026-09-01", "end_date": "2026-09-03"},
            {"terminal": "TERM-A", "start_date": "2026-09-05", "end_date": "2026-09-07"},
        ])
        # 先确认只有第一份区间可核验时提交不了
        assert quality.submit_reverification(batch_key)["ok"] is False

        # 第二份区间在确认前补齐真实点文件，并通过迁移后的独立核验
        _append_backfill_points(
            backfill, batch_key, "TERM-A",
            ["2026-09-05", "2026-09-06", "2026-09-07"])
        rebuilt = backfill.build_interval_proofs_for_existing(batch_key)
        assert len(rebuilt["verified_intervals"]) == 2
        verify = backfill.verify_backfill_complete(batch_key)
        assert verify["is_complete"] is True
        assert len(verify["verified_intervals"]) == 2

        # 待复验时段随核验结果更新为空
        state = quality.get_reverification(batch_key)
        assert state["pending_intervals"] == []

        result = quality.submit_reverification(batch_key)
        assert result["ok"] is True
        cert = result["certificate"]
        # 证书按确认时的联合覆盖生成：两个范围均属实
        confirmed = cert["confirmed_coverage"]
        assert len(confirmed) == 2
        assert confirmed[0] == {
            "terminal": "TERM-A", "start_date": "2026-09-01", "end_date": "2026-09-03"}
        assert confirmed[1] == {
            "terminal": "TERM-A", "start_date": "2026-09-05", "end_date": "2026-09-07"}
        # 开始时的覆盖快照只含第一份区间，留作审计
        assert cert["start_coverage"] == [{
            "terminal": "TERM-A", "start_date": "2026-09-01", "end_date": "2026-09-03"}]
        assert cert["remaining_pending_intervals"] == []
        assert cert["coverage_basis"] == "verified_interval_at_confirmation"
        # 证书绑定了两个区间批次的校验和
        assert len(cert["interval_batches"]) == 2
        assert all(b["checksum"] for b in cert["interval_batches"])

        final_state = quality.get_reverification(batch_key)
        assert final_state["status"] == "submitted"

    def test_replayed_count_receipt_after_migration_cannot_complete_gap(self, setup_data_dir):
        """迁移后再重放一条只有累计值的旧完成通知，缺证区间依旧不能复验完成。"""
        from app.services.geo_source import GeoSourceService
        from app.services.geo_backfill import GeoBackfillService
        from app.services.geo_quality import GeoQualityService

        geo, backfill, quality = (
            GeoSourceService(), GeoBackfillService(), GeoQualityService())
        batch_key = "MIG-REPLAY"
        _onboard(geo, batch_key)
        _append_backfill_points(
            backfill, batch_key, "TERM-A", ["2026-09-01", "2026-09-02"])
        backfill.build_interval_proofs_for_existing(batch_key)

        quality.start_reverification(batch_key, scopes=[
            {"terminal": "TERM-A", "start_date": "2026-09-01", "end_date": "2026-09-05"},
        ])

        backfill.register_completion_receipt(
            batch_key, total_after_backfill=100, backfill_count=5,
            start_date="2026-09-01", end_date="2026-09-05")

        result = quality.submit_reverification(batch_key)
        assert result["ok"] is False
        # 09-03~09-05 没有真实点凭据，必须留在待复验时段
        assert result["pending_intervals"] == [
            {"terminal": "TERM-A", "start_date": "2026-09-03", "end_date": "2026-09-05"}]

        # 区间核验本身也不受回执影响：未核验区间依旧未核验
        verify = backfill.verify_backfill_complete(batch_key)
        assert verify["unverified_intervals"] == []  # 已登记的区间本身已核验
        coverage = backfill.get_verified_coverage(batch_key)
        assert coverage["verified_intervals"][0]["end_date"] == "2026-09-02"
