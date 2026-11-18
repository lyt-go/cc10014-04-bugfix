"""字段映射接口：维护映射规则，覆盖提交校验、标记生效、标记废弃等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.column_mapping import ColumnMappingService

router = APIRouter(prefix="/api/column_mapping", tags=["字段映射"])

service = ColumnMappingService()

LIST_FIELDS = ["规则编号", "源字段名", "目标字段名", "数据类型", "默认值", "转换函数", "规则版本", "生效日期"]
STATUSES = ["草稿", "待校验", "已生效", "已废弃"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已生效、已废弃"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、规则编号关键字与状态过滤字段映射列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="column_mapping.csv"'})


@router.get("/versions/table")
def get_version_table(
    rule_no: str | None = Query(default=None, description="按规则编号过滤版本表"),
    entry_id: int | None = Query(default=None, description="按映射规则id过滤版本表"),
) -> dict[str, object]:
    """字段映射版本表：只保留单向状态推进记录与当前生效版本数据结果。"""
    items = service.get_version_table(rule_no=rule_no, mapping_id=entry_id)
    return {"items": items, "total": len(items)}


@router.get("/versions/effective")
def get_effective_versions() -> dict[str, object]:
    """当前生效版本数据结果：结构注册跨模块读取的数据源。"""
    items = service.get_effective_versions()
    return {"items": items, "total": len(items)}


@router.get("/versions/audit")
def get_audit_trail(
    rule_no: str | None = Query(default=None, description="按规则编号过滤审计留痕"),
) -> dict[str, object]:
    """状态流转、冲突拦截与事务回滚 Bugfix 恢复点的完整留痕。"""
    items = service.get_audit_trail(rule_no=rule_no)
    return {"items": items, "total": len(items)}


@router.get("/{entry_id}/versions")
def get_entry_versions(entry_id: int) -> dict[str, object]:
    """单条映射规则的字段映射版本表（状态时间线，严格单向、不倒序）。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"映射规则 {entry_id} 不存在或已归档")
    items = service.get_version_table(mapping_id=entry_id)
    return {"items": items, "total": len(items), "rule_no": entry.get("规则编号", "")}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条映射规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"映射规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条映射规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="映射规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条映射规则执行提交校验、标记生效、标记废弃；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
