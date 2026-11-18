"""内存数据仓库：支持模块间关联、版本管理、模拟表数据、血缘记录与状态流转。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from app.seed import SEED_ROWS


class TransactionError(RuntimeError):
    """字段映射版本表事务提交失败时抛出。

    触发后 Store 已把所有模块的数据回滚到事务开启前的快照（Bugfix 恢复点），
    调用方可以据此向接口返回可读的失败原因，而不会留下半提交的残留版本。
    """


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 模拟真实表数据（分层存储的实际行数据）
        self._table_data: dict[str, list[dict[str, Any]]] = {}
        # 版本历史: "module:id" -> [{version, timestamp, data}, ...]
        self._versions: dict[str, list[dict[str, Any]]] = {}
        # 血缘边记录
        self._lineage_edges: list[dict[str, Any]] = []
        # 质量稽核结果
        self._quality_results: dict[str, list[dict[str, Any]]] = {}
        # 快照冻结数据: "snapshot_id" -> {table_name, rows, frozen_at}
        self._snapshot_data: dict[str, dict[str, Any]] = {}
        # 归档记录
        self._archive_records: list[dict[str, Any]] = []
        # 补数区间记录
        self._backfill_partitions: dict[str, list[dict[str, Any]]] = {}
        # 字段映射版本表：只保留单向状态推进记录与当前生效版本
        self._mapping_versions: list[dict[str, Any]] = []
        # 字段映射审计日志：状态流转、恢复点、冲突拦截、恢复与切换均在此留痕
        self._mapping_audit: list[dict[str, Any]] = []
        # 结构定义过渡版本：记录旧字段来源说明，保证历史版本不丢失
        self._schema_transition_versions: list[dict[str, Any]] = []
        # 为种子数据中已生效的映射规则补一条生效基线版本，
        # 保证版本表、冲突检测与结构注册面板口径一致
        self._bootstrap_seed_effective_versions()

    def _bootstrap_seed_effective_versions(self) -> None:
        for entry in self._tables.get("column_mapping", []):
            if entry.get("status") != "已生效":
                continue
            record = self.append_mapping_version(
                entry, action="基线生效", previous_status="待校验",
                note="种子数据生效基线版本")
            record["是否当前生效版本"] = True
            entry["当前生效版本"] = True

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---- 模拟真实表数据 ----

    def table_rows(self, table_name: str) -> list[dict[str, Any]]:
        return self._table_data.setdefault(table_name, [])

    def set_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data[table_name] = rows

    def append_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data.setdefault(table_name, []).extend(rows)

    # ---- 版本管理 ----

    def _version_key(self, module: str, entry_id: int) -> str:
        return f"{module}:{entry_id}"

    def save_version(self, module: str, entry_id: int, data: dict[str, Any]) -> dict[str, Any]:
        key = self._version_key(module, entry_id)
        versions = self._versions.setdefault(key, [])
        version = {
            "version": len(versions) + 1,
            "timestamp": datetime.now().isoformat(),
            "data": dict(data),
        }
        versions.append(version)
        return version

    def get_versions(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        key = self._version_key(module, entry_id)
        return list(self._versions.get(key, []))

    def get_latest_version(self, module: str, entry_id: int) -> dict[str, Any] | None:
        versions = self.get_versions(module, entry_id)
        return versions[-1] if versions else None

    # ---- 事务：提交失败整体回滚，保留 Bugfix 恢复点 ----

    @contextmanager
    def transaction(self, label: str = "字段映射版本表事务") -> Iterator[None]:
        """模拟数据库事务：进入时对全部可变数据拍快照，异常时整体回滚。

        字段映射状态推进与结构注册跨模块写入在同一事务内完成；
        一旦事务体抛出异常，所有模块的数据恢复到进入前的 Bugfix 恢复点，
        并在审计日志里追加一条回滚恢复点（审计日志本身不参与回滚，确保留痕）。
        """
        snapshot = {
            "_tables": copy.deepcopy(self._tables),
            "_table_data": copy.deepcopy(self._table_data),
            "_versions": copy.deepcopy(self._versions),
            "_lineage_edges": copy.deepcopy(self._lineage_edges),
            "_quality_results": copy.deepcopy(self._quality_results),
            "_snapshot_data": copy.deepcopy(self._snapshot_data),
            "_archive_records": copy.deepcopy(self._archive_records),
            "_backfill_partitions": copy.deepcopy(self._backfill_partitions),
            "_mapping_versions": copy.deepcopy(self._mapping_versions),
            "_schema_transition_versions": copy.deepcopy(self._schema_transition_versions),
        }
        try:
            yield
        except Exception as exc:  # noqa: BLE001 - 事务边界需要兜底所有异常
            for name, state in snapshot.items():
                setattr(self, name, copy.deepcopy(state))
            self.add_mapping_audit({
                "事件类型": "事务回滚恢复点",
                "说明": f"{label}提交失败，已回滚到事务前状态：{exc}",
                "关联规则": "",
                "目标字段": "",
            })
            if isinstance(exc, TransactionError):
                raise
            raise TransactionError(f"{label}提交失败，数据已回滚：{exc}") from exc

    # ---- 字段映射版本表 ----

    @staticmethod
    def _mapping_target_fields(entry: dict[str, Any]) -> list[str]:
        return [f.strip() for f in str(entry.get("目标字段名", "")).split(",") if f.strip()]

    def append_mapping_version(self, entry: dict[str, Any], *, action: str,
                               previous_status: str, note: str = "") -> dict[str, Any]:
        """追加一条字段映射版本记录。

        版本表只追加、不改写：状态按 草稿→待校验→已生效→已废弃 单向推进，
        任何回退动作在服务层就会被拦下，因此版本表天然不会出现倒序。
        """
        records = self._mapping_versions
        rule_no = str(entry.get("规则编号", ""))
        rule_seq = sum(1 for r in records if r.get("规则编号") == rule_no) + 1
        record = {
            "id": len(records) + 1,
            "规则编号": rule_no,
            "映射规则id": int(entry.get("id", 0)),
            "规则版本": str(entry.get("规则版本", "") or f"v{rule_seq}"),
            "版本序号": rule_seq,
            "源字段名": entry.get("源字段名", ""),
            "目标字段名": entry.get("目标字段名", ""),
            "目标字段列表": self._mapping_target_fields(entry),
            "数据类型": entry.get("数据类型", ""),
            "转换函数": entry.get("转换函数", ""),
            "来源批次": entry.get("来源批次", ""),
            "动作": action,
            "前置状态": previous_status,
            "状态": entry.get("status", ""),
            "是否当前生效版本": entry.get("status") == "已生效",
            "留痕说明": note,
            "记录时间": datetime.now().isoformat(),
        }
        records.append(record)
        return record

    def get_mapping_versions(self, *, rule_no: str | None = None,
                             mapping_id: int | None = None) -> list[dict[str, Any]]:
        """读取字段映射版本表，可按规则编号或映射规则id过滤，返回追加顺序（即时间线顺序）。"""
        result = self._mapping_versions
        if rule_no is not None:
            result = [r for r in result if r.get("规则编号") == rule_no]
        if mapping_id is not None:
            result = [r for r in result if r.get("映射规则id") == mapping_id]
        return [dict(r) for r in result]

    def get_effective_mapping_versions(self) -> list[dict[str, Any]]:
        """当前生效版本数据结果：只返回标记为当前生效的版本记录。"""
        return [dict(r) for r in self._mapping_versions if r.get("是否当前生效版本")]

    def get_effective_mapping_for_target(self, target_field: str) -> dict[str, Any] | None:
        """结构注册调用链使用：返回目标字段当前唯一的生效映射版本。"""
        for record in reversed(self._mapping_versions):
            if record.get("是否当前生效版本") and target_field in record.get("目标字段列表", []):
                return dict(record)
        return None

    def find_conflicting_effective_mappings(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """检查同一目标字段是否已被其它生效规则占用（冲突检测，不处理优先级）。"""
        target_fields = set(self._mapping_target_fields(entry))
        conflicts: list[dict[str, Any]] = []
        for record in self._mapping_versions:
            if not record.get("是否当前生效版本"):
                continue
            if record.get("映射规则id") == int(entry.get("id", 0)):
                continue
            overlap = target_fields.intersection(record.get("目标字段列表", []))
            if overlap:
                conflicts.append({**dict(record), "冲突字段": sorted(overlap)})
        return conflicts

    def revoke_effective_flag(self, mapping_id: int) -> int:
        """规则废弃时回收其版本表里的「当前生效版本」标记。

        只改标记不删记录：历史字段映射版本表不能丢失，而结构注册也不会再
        读到一个已废弃的当前版本。返回收割的记录条数。
        """
        changed = 0
        for record in self._mapping_versions:
            if record.get("映射规则id") == mapping_id and record.get("是否当前生效版本"):
                record["是否当前生效版本"] = False
                changed += 1
        return changed

    # ---- 字段映射审计日志（恢复与切换均留痕） ----

    def add_mapping_audit(self, event: dict[str, Any]) -> dict[str, Any]:
        record = {
            "id": len(self._mapping_audit) + 1,
            "记录时间": datetime.now().isoformat(),
            "事件类型": event.get("事件类型", ""),
            "关联规则": event.get("关联规则", ""),
            "目标字段": event.get("目标字段", ""),
            "说明": event.get("说明", ""),
        }
        self._mapping_audit.append(record)
        return record

    def get_mapping_audit(self, *, rule_no: str | None = None) -> list[dict[str, Any]]:
        if rule_no is None:
            return [dict(r) for r in self._mapping_audit]
        return [dict(r) for r in self._mapping_audit if r.get("关联规则") == rule_no]

    # ---- 结构定义过渡版本（保留旧字段来源说明） ----

    def save_schema_transition(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {
            "id": len(self._schema_transition_versions) + 1,
            "记录时间": datetime.now().isoformat(),
            **record,
        }
        self._schema_transition_versions.append(record)
        return record

    def get_schema_transitions(self, *, table_name: str | None = None) -> list[dict[str, Any]]:
        if table_name is None:
            return [dict(r) for r in self._schema_transition_versions]
        return [dict(r) for r in self._schema_transition_versions if r.get("表名称") == table_name]

    # ---- 血缘 ----

    def add_lineage(self, upstream: str, downstream: str, field_map: str,
                    transform_logic: str = "", batch_id: str = "") -> dict[str, Any]:
        edge = {
            "id": len(self._lineage_edges) + 1,
            "上游表名": upstream,
            "下游表名": downstream,
            "来源字段": field_map,
            "目标字段": field_map,
            "转换逻辑": transform_logic,
            "采集批次": batch_id,
            "链路版本": f"v{len(self._lineage_edges) + 1}",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        self._lineage_edges.append(edge)
        # 同时写入 lineage_track 模块表供查询
        self.rows("lineage_track").append(edge)
        return edge

    def get_lineage_edges(self) -> list[dict[str, Any]]:
        return list(self._lineage_edges)

    # ---- 质量稽核结果 ----

    def save_quality_result(self, rule_id: str, result: dict[str, Any]) -> None:
        self._quality_results.setdefault(rule_id, []).append(result)

    def get_quality_results(self, rule_id: str) -> list[dict[str, Any]]:
        return list(self._quality_results.get(rule_id, []))

    # ---- 快照 ----

    def save_snapshot_data(self, snapshot_id: str, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._snapshot_data[snapshot_id] = {
            "table_name": table_name,
            "rows": [dict(r) for r in rows],
            "frozen_at": datetime.now().isoformat(),
            "row_count": len(rows),
        }

    def get_snapshot_data(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshot_data.get(snapshot_id)

    # ---- 归档 ----

    def add_archive_record(self, record: dict[str, Any]) -> None:
        self._archive_records.append(record)

    def get_archive_records(self) -> list[dict[str, Any]]:
        return list(self._archive_records)

    # ---- 补数分区 ----

    def save_backfill_partition(self, job_id: str, partition: dict[str, Any]) -> None:
        self._backfill_partitions.setdefault(job_id, []).append(partition)

    def get_backfill_partitions(self, job_id: str) -> list[dict[str, Any]]:
        return list(self._backfill_partitions.get(job_id, []))


store = Store()
