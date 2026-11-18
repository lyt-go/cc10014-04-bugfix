"""字段映射业务规则：支持从入库批次提取源字段，映射到目标字段，并驱动ODS表结构创建。

状态机（单向推进，不允许跳跃或回退）：
    草稿 --提交校验--> 待校验 --标记生效--> 已生效 --标记废弃--> 已废弃

修复点：
- 待校验不能直接跳到已废弃，已生效也不能被改回旧状态；
- 每次合法推进都在「字段映射版本表」追加一条只增记录，版本表保留单向状态
  与当前生效版本数据结果；
- 状态推进与结构注册跨模块写入在同一事务内提交，失败整体回滚并保留
  Bugfix 恢复点；
- 同一目标字段上旧映射废弃与新映射生效同时作用时，结构注册生成过渡版本
  并保留旧字段来源说明；缩窄边界后结构定义只保留一个当前字段来源；
- 若同一目标字段同时存在两个生效映射（优先级尚未确定的冲突），生效动作
  会被拦截并说明冲突字段与规则，不做静默覆盖。
"""
from __future__ import annotations

from typing import Any

from app.store import TransactionError, store

MODULE = "column_mapping"
LIST_FIELDS = ["规则编号", "源字段名", "目标字段名", "数据类型", "默认值", "转换函数", "规则版本", "生效日期"]
REQUIRED_FIELDS = ["规则编号", "源字段名", "目标字段名"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
# 每个状态只允许推进到下一个相邻状态，保证严格单向
NEXT_STATUS: dict[str, str] = {
    "草稿": "待校验",
    "待校验": "已生效",
    "已生效": "已废弃",
}
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class ColumnMappingService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["当前生效版本"] = False
        # 关联来源批次
        if values.get("来源批次"):
            entry["来源批次"] = values["来源批次"]
        rows.append(entry)
        store.add_mapping_audit({
            "事件类型": "规则登记",
            "关联规则": entry.get("规则编号", ""),
            "目标字段": entry.get("目标字段名", ""),
            "说明": f"映射规则登记，初始状态为{STATUS_ORDER[0]}",
        })
        return entry, []

    # ---- 状态流转：严格单向，事务提交，版本表留痕 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"映射规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于字段映射可执行范围"

        current_status = str(entry.get("status", ""))
        target = ACTION_RULES[action]
        rule_no = str(entry.get("规则编号", ""))

        # 单向状态机：只允许推进到相邻的下一个状态
        allowed = NEXT_STATUS.get(current_status)
        if allowed is None:
            return None, f"状态不能继续推进：{rule_no} 当前为{current_status}，已是终态"
        if target != allowed:
            store.add_mapping_audit({
                "事件类型": "非法流转拦截",
                "关联规则": rule_no,
                "目标字段": str(entry.get("目标字段名", "")),
                "说明": f"拦截 {current_status}→{target}，单向状态机只允许 {current_status}→{allowed}",
            })
            return None, (
                f"状态流转必须按单向顺序落库：{rule_no} 当前为{current_status}，"
                f"不能执行「{action}」跳到{target}，只允许推进到{allowed}"
            )

        # 生效前的冲突检测：同目标字段已有其它生效规则且未先废弃时拦截
        if target == "已生效":
            conflicts = store.find_conflicting_effective_mappings(entry)
            if conflicts:
                detail = "；".join(
                    f"字段{'、'.join(c.get('冲突字段', []))} 已被规则 {c.get('规则编号')} 的生效版本占用"
                    for c in conflicts
                )
                store.add_mapping_audit({
                    "事件类型": "冲突拦截",
                    "关联规则": rule_no,
                    "目标字段": str(entry.get("目标字段名", "")),
                    "说明": f"生效冲突，优先级尚未确定，已拦截：{detail}",
                })
                return None, (
                    f"字段映射冲突：{detail}。请先将旧规则标记废弃再生效新规则；"
                    "冲突版本的优先顺序尚未确定，平台不会静默覆盖当前生效版本。"
                )

        previous_status = current_status
        try:
            with store.transaction(label=f"字段映射{rule_no}{action}"):
                entry["status"] = target
                entry["pending"] = target != STATUS_ORDER[-1]
                entry["abnormal"] = action in NEGATIVE_ACTIONS
                entry["当前生效版本"] = target == "已生效"
                # 废弃时回收该规则在版本表里的「当前生效版本」标记，
                # 避免结构注册读到一个已废弃的当前版本
                if target == "已废弃":
                    self._revoke_effective_flag(entry_id)
                # 通用版本历史保持兼容
                store.save_version(MODULE, entry_id, entry)
                # 字段映射版本表：只增、单向、标记当前生效版本
                record = store.append_mapping_version(
                    entry, action=action, previous_status=previous_status,
                    note=f"{previous_status}→{target} 单向推进")
                # 跨模块：结构注册按字段映射调用链同步结构定义
                self._sync_schema_registry(entry, action, target, record)
        except TransactionError as exc:
            # 事务已回滚到 Bugfix 恢复点，规则状态保持流转前
            return None, str(exc)

        store.add_mapping_audit({
            "事件类型": "状态流转",
            "关联规则": rule_no,
            "目标字段": str(entry.get("目标字段名", "")),
            "说明": f"{action}成功：{previous_status}→{target}，版本表序号{record['版本序号']}",
        })
        return entry, f"映射规则已{action}"

    def _sync_schema_registry(self, entry: dict[str, Any], action: str,
                              target: str, version_record: dict[str, Any]) -> None:
        """结构注册跨模块写入：与状态推进在同一事务内，失败一起回滚。"""
        from app.services.schema_registry import SchemaRegistryService

        schema_service = SchemaRegistryService()
        table_name = self.resolve_target_table(entry)
        if target == "已生效":
            schema_service.apply_effective_mapping(entry, version_record, table_name)
        elif target == "已废弃":
            schema_service.apply_deprecated_mapping(entry, version_record, table_name)

    @staticmethod
    def resolve_target_table(entry: dict[str, Any]) -> str:
        """按字段映射调用链推导目标表：来源批次 → ODS 表名，缺省回退到未登记占位。"""
        from app.services.schema_registry import UNRESOLVED_TABLE

        batch_no = str(entry.get("来源批次", ""))
        if batch_no:
            ods = store.find_by_field("ods_layer", "创建批次", batch_no)
            if ods and ods.get("表名称"):
                return str(ods["表名称"])
        return batch_no or UNRESOLVED_TABLE

    @staticmethod
    def _revoke_effective_flag(entry_id: int) -> None:
        store.revoke_effective_flag(entry_id)

    def get_mappings_by_batch(self, batch_no: str) -> list[dict[str, Any]]:
        return store.find_all_by_field(MODULE, "来源批次", batch_no)

    # ---- 字段映射版本表查询 ----

    def get_version_table(self, *, rule_no: str | None = None,
                          mapping_id: int | None = None) -> list[dict[str, Any]]:
        """字段映射版本表：保留单向状态记录与当前生效版本数据结果。"""
        return store.get_mapping_versions(rule_no=rule_no, mapping_id=mapping_id)

    def get_effective_versions(self) -> list[dict[str, Any]]:
        """当前生效版本数据结果。"""
        return store.get_effective_mapping_versions()

    def get_audit_trail(self, *, rule_no: str | None = None) -> list[dict[str, Any]]:
        """状态流转、冲突拦截、事务回滚恢复点、恢复与切换的完整留痕。"""
        return store.get_mapping_audit(rule_no=rule_no)
