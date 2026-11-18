"""字段映射状态流转修复与字段映射版本表测试。

覆盖验收点：
- 状态推进严格按单向顺序落库：待校验不能直接跳已废弃，已生效不能改回旧状态；
- 字段映射版本表只增不倒序，保留单向状态与当前生效版本数据结果；
- 版本表事务提交失败整体回滚，并保留 Bugfix 恢复点留痕；
- 同一目标字段生效冲突会被拦截（优先级未确定，不静默覆盖）；
- 旧映射废弃+新映射生效后，结构注册每个目标字段只有一个当前来源，
  过渡版本保留旧字段来源说明，历史版本不丢失；
- 字段映射入口与结构注册面板读到的字段映射结果一致。
"""
from __future__ import annotations

import pytest

from app.seed import SEED_ROWS
from app.store import store
from app.services.column_mapping import ColumnMappingService
from app.services.raw_ingest import RawIngestService
from app.services.schema_registry import SchemaRegistryService


def reset_store():
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data.clear()
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()
    store._mapping_versions.clear()
    store._mapping_audit.clear()
    store._schema_transition_versions.clear()


@pytest.fixture()
def draft_mapping():
    """经入库链路生成一条草稿状态的字段映射规则。"""
    reset_store()
    ri = RawIngestService()
    batch, _ = ri.create_entry({
        "批次编号": "RAW-FIX-01", "来源系统": "订单系统", "文件格式": "CSV", "原始行数": "10",
    })
    ri.run_action(batch["id"], "确认入库")
    cms = ColumnMappingService()
    mapping = cms.get_mappings_by_batch("RAW-FIX-01")[0]
    yield cms, mapping
    reset_store()


class TestUnidirectionalStatusFlow:
    def test_pending_cannot_jump_to_deprecated(self, draft_mapping):
        cms, mapping = draft_mapping
        cms.run_action(mapping["id"], "提交校验")
        assert cms.get_entry(mapping["id"])["status"] == "待校验"

        entry, msg = cms.run_action(mapping["id"], "标记废弃")
        assert entry is None
        assert "单向顺序" in msg
        # 被拦截后状态与版本表都不能留下残留
        assert cms.get_entry(mapping["id"])["status"] == "待校验"
        assert [v["状态"] for v in cms.get_version_table(mapping_id=mapping["id"])] == ["待校验"]

    def test_draft_cannot_jump_to_effective(self, draft_mapping):
        cms, mapping = draft_mapping
        entry, msg = cms.run_action(mapping["id"], "标记生效")
        assert entry is None
        assert cms.get_entry(mapping["id"])["status"] == "草稿"
        assert cms.get_version_table() == []

    def test_effective_cannot_revert_to_previous_status(self, draft_mapping):
        cms, mapping = draft_mapping
        cms.run_action(mapping["id"], "提交校验")
        cms.run_action(mapping["id"], "标记生效")
        assert cms.get_entry(mapping["id"])["status"] == "已生效"

        for action in ("提交校验", "标记生效"):
            entry, msg = cms.run_action(mapping["id"], action)
            assert entry is None
            assert "单向顺序" in msg
        assert cms.get_entry(mapping["id"])["status"] == "已生效"

    def test_deprecated_is_terminal(self, draft_mapping):
        cms, mapping = draft_mapping
        cms.run_action(mapping["id"], "提交校验")
        cms.run_action(mapping["id"], "标记生效")
        cms.run_action(mapping["id"], "标记废弃")
        assert cms.get_entry(mapping["id"])["status"] == "已废弃"

        for action in ("提交校验", "标记生效", "标记废弃"):
            entry, msg = cms.run_action(mapping["id"], action)
            assert entry is None
            assert "终态" in msg

    def test_full_forward_flow_persists_in_order(self, draft_mapping):
        cms, mapping = draft_mapping
        for action, expected in (("提交校验", "待校验"), ("标记生效", "已生效"), ("标记废弃", "已废弃")):
            entry, msg = cms.run_action(mapping["id"], action)
            assert entry is not None, msg
            assert entry["status"] == expected

        versions = cms.get_version_table(mapping_id=mapping["id"])
        assert [v["状态"] for v in versions] == ["待校验", "已生效", "已废弃"]
        assert [v["前置状态"] for v in versions] == ["草稿", "待校验", "已生效"]
        assert [v["版本序号"] for v in versions] == [1, 2, 3]
        # 废弃后不再有当前生效标记
        assert all(not v["是否当前生效版本"] for v in versions)
        # 通用版本历史同步保留
        assert len(store.get_versions("column_mapping", mapping["id"])) == 3


class TestMappingVersionTable:
    def test_effective_version_flag_and_query(self, draft_mapping):
        cms, mapping = draft_mapping
        cms.run_action(mapping["id"], "提交校验")
        cms.run_action(mapping["id"], "标记生效")

        effective = cms.get_effective_versions()
        assert len(effective) >= 1
        current = [v for v in effective if v["映射规则id"] == mapping["id"]]
        assert len(current) == 1
        assert current[0]["是否当前生效版本"] is True
        assert current[0]["目标字段列表"] == ["order_id", "order_amount", "order_status"]

        # 按规则编号与按id过滤结果一致
        by_no = cms.get_version_table(rule_no=mapping["规则编号"])
        by_id = cms.get_version_table(mapping_id=mapping["id"])
        assert by_no == by_id

    def test_audit_trail_records_interceptions_and_flow(self, draft_mapping):
        cms, mapping = draft_mapping
        cms.run_action(mapping["id"], "标记废弃")  # 草稿直接废弃 -> 拦截
        cms.run_action(mapping["id"], "提交校验")
        cms.run_action(mapping["id"], "标记生效")

        audits = cms.get_audit_trail(rule_no=mapping["规则编号"])
        kinds = [a["事件类型"] for a in audits]
        assert "规则登记" in kinds
        assert "非法流转拦截" in kinds
        assert "状态流转" in kinds


class TestTransactionRollback:
    def test_commit_failure_rolls_back_and_keeps_recovery_point(self, draft_mapping):
        cms, mapping = draft_mapping
        cms.run_action(mapping["id"], "提交校验")
        cms.run_action(mapping["id"], "标记生效")

        status_before = cms.get_entry(mapping["id"])["status"]
        versions_before = len(cms.get_version_table())
        original = store.append_mapping_version

        def broken_append(*args, **kwargs):
            if kwargs.get("action") == "标记废弃":
                raise RuntimeError("模拟版本表事务提交失败")
            return original(*args, **kwargs)

        store.append_mapping_version = broken_append
        try:
            entry, msg = cms.run_action(mapping["id"], "标记废弃")
        finally:
            store.append_mapping_version = original

        assert entry is None
        assert "回滚" in msg
        # 数据回到事务前的 Bugfix 恢复点
        assert cms.get_entry(mapping["id"])["status"] == status_before
        assert len(cms.get_version_table()) == versions_before
        # 恢复点留痕（审计日志不参与回滚）
        audits = cms.get_audit_trail()
        recovery = [a for a in audits if a["事件类型"] == "事务回滚恢复点"]
        assert len(recovery) == 1
        assert "标记废弃" in recovery[0]["说明"]

    def test_rollback_also_reverts_schema_registry_write(self, draft_mapping):
        cms, mapping = draft_mapping
        cms.run_action(mapping["id"], "提交校验")
        schema_before = len(store.rows("schema_registry"))

        original = store.save_schema_transition

        def broken_transition(*args, **kwargs):
            raise RuntimeError("模拟结构注册跨模块写入失败")

        store.save_schema_transition = broken_transition
        try:
            entry, msg = cms.run_action(mapping["id"], "标记生效")
        finally:
            store.save_schema_transition = original

        assert entry is None
        assert "回滚" in msg
        assert cms.get_entry(mapping["id"])["status"] == "待校验"
        assert len(store.rows("schema_registry")) == schema_before
        assert cms.get_effective_versions() == []


class TestConflictAndSchemaSync:
    def test_duplicate_effective_mapping_is_blocked(self, draft_mapping):
        cms, mapping = draft_mapping
        cms.run_action(mapping["id"], "提交校验")
        cms.run_action(mapping["id"], "标记生效")

        competitor, missing = cms.create_entry({
            "规则编号": "MAP-FIX-02",
            "源字段名": "oid,amt,st",
            "目标字段名": "order_id,order_amount,order_status",
            "数据类型": "STRING,DECIMAL,STRING",
            "规则版本": "v1",
            "来源批次": "RAW-FIX-01",
        })
        assert missing == []
        cms.run_action(competitor["id"], "提交校验")
        entry, msg = cms.run_action(competitor["id"], "标记生效")
        assert entry is None
        assert "冲突" in msg
        assert "优先顺序尚未确定" in msg
        # 冲突被拦截，不产生生效版本残留
        assert cms.get_entry(competitor["id"])["status"] == "待校验"
        assert all(v["映射规则id"] != competitor["id"] for v in cms.get_effective_versions())
        assert any(a["事件类型"] == "冲突拦截" for a in cms.get_audit_trail())

    def test_deprecate_then_activate_leaves_single_current_source(self, draft_mapping):
        cms, mapping = draft_mapping
        sch = SchemaRegistryService()
        cms.run_action(mapping["id"], "提交校验")
        cms.run_action(mapping["id"], "标记生效")

        new_rule, _ = cms.create_entry({
            "规则编号": "MAP-FIX-02",
            "源字段名": "oid,amt,st,channel",
            "目标字段名": "order_id,order_amount,order_status,pay_channel",
            "数据类型": "STRING,DECIMAL,STRING,STRING",
            "规则版本": "v2",
            "来源批次": "RAW-FIX-01",
        })
        cms.run_action(new_rule["id"], "提交校验")
        # 先废弃旧规则，再生效新规则（边界扩大，含新字段 pay_channel）
        old_entry, msg = cms.run_action(mapping["id"], "标记废弃")
        assert old_entry is not None, msg
        new_entry, msg = cms.run_action(new_rule["id"], "标记生效")
        assert new_entry is not None, msg

        # 结构注册面板：每个目标字段只有一个当前来源，且指向新规则
        sources = sch.get_current_field_sources("ods_orders")
        keys = [(s["表名称"], s["目标字段"]) for s in sources]
        assert len(keys) == len(set(keys))
        assert {s["目标字段"] for s in sources} == {
            "order_id", "order_amount", "order_status", "pay_channel"}
        assert all(s["规则编号"] == "MAP-FIX-02" for s in sources)

        # 过渡版本保留旧字段来源说明，且包含边界扩大与缩窄两类留痕
        transitions = sch.get_transition_versions("ods_orders")
        kinds = {t["变更类型"] for t in transitions}
        assert "边界缩窄" in kinds
        assert "边界扩大-过渡版本" in kinds
        described = [t for t in transitions if t.get("旧字段来源说明")]
        assert described, "过渡版本必须保留旧字段来源说明"

        # 历史字段映射版本表不能丢失
        old_versions = cms.get_version_table(mapping_id=mapping["id"])
        assert [v["状态"] for v in old_versions] == ["待校验", "已生效", "已废弃"]
        new_versions = cms.get_version_table(mapping_id=new_rule["id"])
        assert [v["状态"] for v in new_versions] == ["待校验", "已生效"]

        # 字段映射入口与结构注册面板读到的结果一致
        effective_rules = {v["规则编号"] for v in cms.get_effective_versions()}
        source_rules = {s["规则编号"] for s in sources}
        assert source_rules <= effective_rules

    def test_refresh_after_switch_keeps_timeline_in_order(self, draft_mapping):
        cms, mapping = draft_mapping
        sch = SchemaRegistryService()
        cms.run_action(mapping["id"], "提交校验")
        cms.run_action(mapping["id"], "标记生效")
        cms.run_action(mapping["id"], "标记废弃")

        # 重新读取（模拟从版本表列表重新打开后刷新结构注册页面）
        reloaded_versions = cms.get_version_table(mapping_id=mapping["id"])
        ids = [v["id"] for v in reloaded_versions]
        assert ids == sorted(ids)
        statuses = [v["状态"] for v in reloaded_versions]
        assert statuses == ["待校验", "已生效", "已废弃"]
        # 已废弃规则不再提供当前字段来源
        assert sch.get_current_field_sources("ods_orders") == []

    def test_rule_without_registered_table_does_not_create_schema_row(self):
        """UI 登记的映射没有来源批次时，生效只留痕，不向结构注册表写行。"""
        reset_store()
        cms = ColumnMappingService()
        sch = SchemaRegistryService()
        schema_before = len(store.rows("schema_registry"))

        rule, missing = cms.create_entry({
            "规则编号": "MAP-NO-TABLE-01",
            "源字段名": "col_a",
            "目标字段名": "field_a",
            "数据类型": "STRING",
            "规则版本": "v1",
        })
        assert missing == []
        assert cms.run_action(rule["id"], "提交校验")[0] is not None
        entry, msg = cms.run_action(rule["id"], "标记生效")
        assert entry is not None, msg

        # 结构注册表行数不变，未注册目标表不出现在字段来源面板
        assert len(store.rows("schema_registry")) == schema_before
        assert sch.get_current_field_sources() == []
        # 但过渡留痕与版本表仍然保留
        transitions = sch.get_transition_versions()
        assert any(t["变更类型"] == "目标表未注册-仅留痕" for t in transitions)
        assert cms.get_version_table(mapping_id=rule["id"])[-1]["状态"] == "已生效"
