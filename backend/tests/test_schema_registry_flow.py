"""结构注册状态机单向守卫测试。"""
from __future__ import annotations

import pytest

from app.seed import SEED_ROWS
from app.store import store
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


class TestSchemaRegistryStatusFlow:
    def setup_method(self):
        reset_store()
        self.sch = SchemaRegistryService()
        entry, missing = self.sch.create_entry({
            "定义编号": "SCH-FLOW-01",
            "表名称": "ods_flow_demo",
            "字段名称": "col_a,col_b",
            "字段类型": "STRING,STRING",
            "定义版本": "v1",
            "生效批次": "RAW-FLOW-01",
        })
        assert missing == []
        self.entry_id = entry["id"]

    def teardown_method(self):
        reset_store()

    def test_forward_flow_allowed(self):
        entry, msg = self.sch.run_action(self.entry_id, "提交校验")
        assert entry is not None and entry["status"] == "待校验", msg
        entry, msg = self.sch.run_action(self.entry_id, "标记生效")
        assert entry is not None and entry["status"] == "已生效", msg
        entry, msg = self.sch.run_action(self.entry_id, "标记废弃")
        assert entry is not None and entry["status"] == "已废弃", msg

    def test_skip_and_revert_blocked(self):
        # 草稿不能直接生效/废弃
        entry, _ = self.sch.run_action(self.entry_id, "标记生效")
        assert entry is None
        entry, _ = self.sch.run_action(self.entry_id, "标记废弃")
        assert entry is None
        self.sch.run_action(self.entry_id, "提交校验")
        # 待校验不能直接废弃
        entry, msg = self.sch.run_action(self.entry_id, "标记废弃")
        assert entry is None and "单向顺序" in msg
        # 不能回退到草稿（无该动作，但重复提交校验属于原地跳，也被拦）
        entry, msg = self.sch.run_action(self.entry_id, "提交校验")
        assert entry is None and "单向顺序" in msg
        assert self.sch.get_entry(self.entry_id)["status"] == "待校验"

    def test_deprecated_is_terminal(self):
        self.sch.run_action(self.entry_id, "提交校验")
        self.sch.run_action(self.entry_id, "标记生效")
        self.sch.run_action(self.entry_id, "标记废弃")
        for action in ("提交校验", "标记生效", "标记废弃"):
            entry, msg = self.sch.run_action(self.entry_id, action)
            assert entry is None and "终态" in msg
