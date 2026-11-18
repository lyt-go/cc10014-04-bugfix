"""结构定义业务规则：管理表的字段结构，版本化管理。

与字段映射版本表的联动（跨模块写入，运行在字段映射的同一事务内）：
- 新映射生效：按当前生效映射重建结构定义，同一目标字段只保留一个当前来源；
  边界扩大（新增字段）时生成过渡版本并保留旧字段来源说明；
- 旧映射废弃：缩窄结构定义边界，移除该映射提供的字段，历史结构版本不丢失；
- 所有过渡版本通过 save_schema_transition 留痕，状态时间线只增不倒序。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "schema_registry"
LIST_FIELDS = ["定义编号", "表名称", "字段名称", "字段类型", "可空标识", "默认值", "定义版本", "生效批次"]
REQUIRED_FIELDS = ["定义编号", "表名称", "字段名称"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
# 单向状态机：每个状态只能推进到相邻下一状态
NEXT_STATUS: dict[str, str] = {
    "草稿": "待校验",
    "待校验": "已生效",
    "已生效": "已废弃",
}
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []
# 映射规则没有可解析的注册目标表时使用的占位值：只留痕，不生成结构定义行
UNRESOLVED_TABLE = "未登记目标表"


class SchemaRegistryService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("定义编号", "")).casefold()]
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
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"结构定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于结构注册可执行范围"
        target = ACTION_RULES[action]
        # 单向状态机：禁止跳跃与回退，避免已生效定义被改回旧状态
        current_status = str(entry.get("status", ""))
        allowed = NEXT_STATUS.get(current_status)
        if allowed is None:
            return None, f"状态不能继续推进：结构定义 {entry_id} 当前为{current_status}，已是终态"
        if target != allowed:
            return None, (
                f"状态流转必须按单向顺序落库：结构定义 {entry_id} 当前为{current_status}，"
                f"不能执行「{action}」跳到{target}，只允许推进到{allowed}"
            )
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"结构定义已{action}"

    def get_schema_for_table(self, table_name: str) -> list[dict[str, Any]]:
        return store.find_all_by_field(MODULE, "表名称", table_name)

    def get_latest_schema(self, table_name: str) -> dict[str, Any] | None:
        schemas = self.get_schema_for_table(table_name)
        if not schemas:
            return None
        # 找版本号最大的
        latest = max(schemas, key=lambda s: s.get("定义版本", "v0"))
        return latest

    def derive_schema_from_lineage(self, table_name: str) -> dict[str, Any] | None:
        """从血缘关系推导字段变更：找上游表最新schema并合并。"""
        # 查找血缘中该table_name作为下游的边
        upstream_tables = []
        for edge in store.get_lineage_edges():
            if edge.get("下游表名") == table_name:
                up = edge.get("上游表名", "")
                if up:
                    upstream_tables.append(up)
        if not upstream_tables:
            return None
        # 取第一个上游的最新schema
        up_schema = self.get_latest_schema(upstream_tables[0])
        if not up_schema:
            return None
        latest = self.get_latest_schema(table_name)
        new_version = "v2"
        if latest:
            try:
                vnum = int(latest.get("定义版本", "v1").replace("v", ""))
                new_version = f"v{vnum + 1}"
            except Exception:
                new_version = "v2"
        # 生成新schema定义
        new_entry = {
            "定义编号": f"SCH-DERIVED-{table_name}",
            "表名称": table_name,
            "字段名称": up_schema.get("字段名称", ""),
            "字段类型": up_schema.get("字段类型", ""),
            "可空标识": up_schema.get("可空标识", ""),
            "默认值": up_schema.get("默认值", ""),
            "定义版本": new_version,
            "生效批次": up_schema.get("生效批次", ""),
        }
        return self.create_entry(new_entry)[0]

    # ---- 字段映射版本表驱动的结构注册 ----

    @staticmethod
    def _split(value: Any) -> list[str]:
        return [part.strip() for part in str(value or "").split(",") if part.strip()]

    def _next_definition_version(self, table_name: str) -> str:
        latest = self.get_latest_schema(table_name)
        if not latest:
            return "v1"
        try:
            return f"v{int(str(latest.get('定义版本', 'v0')).replace('v', '')) + 1}"
        except ValueError:
            return "v1"

    def _current_field_sources(self, table_name: str) -> dict[str, dict[str, str]]:
        """从字段映射版本表读取该表每个目标字段当前唯一的生效来源。"""
        sources: dict[str, dict[str, str]] = {}
        for record in store.get_effective_mapping_versions():
            if self.resolve_table_for_rule(record) != table_name:
                continue
            for field in record.get("目标字段列表", []):
                # 版本表按时间追加，后生效的记录覆盖先前者，保证每个字段只有一个当前来源
                sources[field] = {
                    "规则编号": str(record.get("规则编号", "")),
                    "源字段名": str(record.get("源字段名", "")),
                    "来源批次": str(record.get("来源批次", "")),
                    "规则版本": str(record.get("规则版本", "")),
                }
        return sources

    @staticmethod
    def resolve_table_for_rule(record: dict[str, Any]) -> str:
        batch_no = str(record.get("来源批次", ""))
        if batch_no:
            ods = store.find_by_field("ods_layer", "创建批次", batch_no)
            if ods and ods.get("表名称"):
                return str(ods["表名称"])
        return batch_no or UNRESOLVED_TABLE

    def _create_system_schema(self, table_name: str, field_names: list[str],
                              field_types: list[str], batch_no: str,
                              definition_no: str, defaults: list[str]) -> dict[str, Any]:
        """跨模块系统写入：直接落为已生效的结构定义版本，并保留通用版本历史。"""
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({
            "定义编号": definition_no,
            "表名称": table_name,
            "字段名称": ",".join(field_names),
            "字段类型": ",".join(field_types[:len(field_names)]),
            "可空标识": ",".join(["N"] * len(field_names)),
            "默认值": ",".join(defaults[:len(field_names)]),
            "定义版本": self._next_definition_version(table_name),
            "生效批次": batch_no,
        })
        entry["status"] = "已生效"
        entry["pending"] = False
        entry["abnormal"] = False
        rows.append(entry)
        store.save_version(MODULE, entry["id"], entry)
        return entry

    def apply_effective_mapping(self, mapping_entry: dict[str, Any],
                                version_record: dict[str, Any], table_name: str) -> dict[str, Any]:
        """新映射生效：重建结构定义。

        - 目标表未注册（映射无来源批次）：只做过渡留痕，不生成结构定义行；
        - 边界扩大（出现新目标字段）：生成过渡版本，过渡说明里保留旧字段来源；
        - 缩窄/替换：结构定义只保留每个目标字段的一个当前来源；
        - 历史结构定义不删除，字段映射版本表与结构版本均可回溯。
        """
        if table_name == UNRESOLVED_TABLE:
            transition = store.save_schema_transition({
                "表名称": UNRESOLVED_TABLE,
                "变更类型": "目标表未注册-仅留痕",
                "新增字段": version_record.get("目标字段列表", []),
                "保留字段": [],
                "旧字段来源说明": [],
                "当前字段来源": {},
                "触发规则": str(mapping_entry.get("规则编号", "")),
                "触发动作": "标记生效",
                "映射版本序号": version_record.get("版本序号"),
            })
            return {"跳过结构定义": True, "过渡版本id": transition["id"]}

        previous = self.get_latest_schema(table_name)
        previous_fields = set(self._split(previous.get("字段名称")) if previous else [])
        new_rule_fields = version_record.get("目标字段列表", [])
        added = [f for f in new_rule_fields if f not in previous_fields]

        # 当前生效来源以字段映射版本表为准，每个目标字段只会得到一个来源
        current_sources = self._current_field_sources(table_name)
        field_names = list(current_sources.keys())
        field_types = self._collect_types(table_name, field_names)
        defaults = self._collect_defaults(table_name, field_names)

        definition_no = f"SCH-{table_name}-{self._next_definition_version(table_name)}"
        schema_entry = self._create_system_schema(
            table_name, field_names, field_types,
            str(mapping_entry.get("来源批次", "")), definition_no, defaults)

        transition = store.save_schema_transition({
            "表名称": table_name,
            "结构定义编号": schema_entry["定义编号"],
            "定义版本": schema_entry["定义版本"],
            "变更类型": "边界扩大-过渡版本" if added else "来源切换",
            "新增字段": added,
            "保留字段": [f for f in field_names if f in previous_fields],
            "旧字段来源说明": self._describe_previous_sources(previous),
            "当前字段来源": current_sources,
            "触发规则": str(mapping_entry.get("规则编号", "")),
            "触发动作": "标记生效",
            "映射版本序号": version_record.get("版本序号"),
        })
        schema_entry["过渡版本id"] = transition["id"]
        return schema_entry

    def apply_deprecated_mapping(self, mapping_entry: dict[str, Any],
                                 version_record: dict[str, Any], table_name: str) -> dict[str, Any] | None:
        """旧映射废弃：缩窄结构定义边界，移除该映射提供的字段。"""
        previous = self.get_latest_schema(table_name)
        deprecated_fields = version_record.get("目标字段列表", [])
        current_sources = self._current_field_sources(table_name)
        # 废弃记录不再是生效版本，current_sources 已自动剔除该规则的字段
        remaining_fields = list(current_sources.keys())

        transition_payload = {
            "表名称": table_name,
            "变更类型": "边界缩窄",
            "移除字段": deprecated_fields,
            "保留字段": remaining_fields,
            "旧字段来源说明": self._describe_previous_sources(previous),
            "当前字段来源": current_sources,
            "触发规则": str(mapping_entry.get("规则编号", "")),
            "触发动作": "标记废弃",
            "映射版本序号": version_record.get("版本序号"),
        }

        if not remaining_fields:
            # 表已无当前生效字段来源，只留过渡留痕，不生成空结构定义
            transition = store.save_schema_transition(transition_payload)
            return {"跳过结构定义": True, "过渡版本id": transition["id"]}

        field_types = self._collect_types(table_name, remaining_fields)
        defaults = self._collect_defaults(table_name, remaining_fields)
        definition_no = f"SCH-{table_name}-{self._next_definition_version(table_name)}"
        schema_entry = self._create_system_schema(
            table_name, remaining_fields, field_types,
            str(mapping_entry.get("来源批次", "")), definition_no, defaults)
        transition_payload["结构定义编号"] = schema_entry["定义编号"]
        transition_payload["定义版本"] = schema_entry["定义版本"]
        transition = store.save_schema_transition(transition_payload)
        schema_entry["过渡版本id"] = transition["id"]
        return schema_entry

    def _describe_previous_sources(self, previous: dict[str, Any] | None) -> list[dict[str, str]]:
        """保留旧字段来源说明：基于上一版结构定义与当时的生效映射。"""
        if not previous:
            return []
        result = []
        for field in self._split(previous.get("字段名称")):
            record = store.get_effective_mapping_for_target(field)
            result.append({
                "字段名称": field,
                "旧结构版本": str(previous.get("定义版本", "")),
                "旧来源规则": str(record.get("规则编号", "")) if record else "历史结构定义（映射已废弃）",
                "旧来源批次": str(record.get("来源批次", "")) if record else str(previous.get("生效批次", "")),
            })
        return result

    def _collect_types(self, table_name: str, field_names: list[str]) -> list[str]:
        """从字段映射版本表的生效记录里汇总每个字段的数据类型。"""
        type_map: dict[str, str] = {}
        for record in store.get_effective_mapping_versions():
            if self.resolve_table_for_rule(record) != table_name:
                continue
            targets = record.get("目标字段列表", [])
            types = self._split(record.get("数据类型"))
            for index, field in enumerate(targets):
                if field in field_names and index < len(types):
                    type_map[field] = types[index]
        return [type_map.get(field, "STRING") for field in field_names]

    def _collect_defaults(self, table_name: str, field_names: list[str]) -> list[str]:
        default_map: dict[str, str] = {}
        for mapping in store.rows("column_mapping"):
            if self.resolve_table_for_rule({
                "来源批次": mapping.get("来源批次", ""),
            }) != table_name:
                continue
            targets = self._split(mapping.get("目标字段名"))
            defaults = self._split(mapping.get("默认值"))
            for index, field in enumerate(targets):
                if field in field_names and index < len(defaults):
                    default_map[field] = defaults[index]
        return [default_map.get(field, "") for field in field_names]

    # ---- 页面核对：目标字段当前来源（唯一） ----

    def get_current_field_sources(self, table_name: str | None = None) -> list[dict[str, Any]]:
        """结构注册面板核对用：每个目标字段只返回一个当前来源。"""
        if table_name is not None:
            tables = {table_name}
        else:
            tables = {
                self.resolve_table_for_rule(record)
                for record in store.get_effective_mapping_versions()
            }
        # 未注册目标表的映射只留痕，不出现在结构注册面板
        tables.discard(UNRESOLVED_TABLE)
        result: list[dict[str, Any]] = []
        for name in sorted(t for t in tables if t):
            for field, source in self._current_field_sources(name).items():
                result.append({"表名称": name, "目标字段": field, **source})
        return result

    def get_transition_versions(self, table_name: str | None = None) -> list[dict[str, Any]]:
        """结构定义过渡版本列表：恢复与切换均留痕，按追加顺序返回。"""
        return store.get_schema_transitions(table_name=table_name)
