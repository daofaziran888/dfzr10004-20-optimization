"""采集存储策略接口：维护存储配置，覆盖提交校验、启用配置、停用配置与归档执行。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.archive import ArchiveService
from app.services.storage import StorageService

router = APIRouter(prefix="/api/storage", tags=["采集存储策略"])

service = StorageService()
archive_service = ArchiveService()

LIST_FIELDS = ["配置编号", "存储路径", "文件格式", "分片大小", "压缩算法", "保留天数", "归档策略", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤采集存储策略列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集存储策略清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "storage", "total": total, "items": items}


@router.get("/archives", response_model=PageResult[dict])
def list_archive_batches(
    config: str | None = Query(default=None, description="按配置编号过滤归档批次"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """归档批次列表：批次状态、已处理分片与容量回收结论都持久保存，刷新后一致。"""
    batches = archive_service.list_batches(config)
    start = max(page - 1, 0) * size
    return PageResult(items=batches[start:start + size], total=len(batches), page=page, size=size)


@router.get("/archives/summary")
def archive_summary() -> dict[str, Any]:
    """容量回收结论汇总：存储配置页与采集审计台账展示同一份数据。"""
    return archive_service.capacity_summary()


@router.get("/archives/{batch_code}")
def get_archive_batch(batch_code: str) -> dict[str, Any]:
    """归档批次详情：已处理分片、失败文件、归档前校验结果与源分片状态。"""
    batch = archive_service.get_batch(batch_code)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"归档批次 {batch_code} 不存在")
    return batch


@router.get("/shards")
def list_shards(config: str | None = Query(default=None, description="按配置编号过滤分片")) -> dict[str, Any]:
    """源分片清单：在线、已迁移、已清理状态随归档批次流转。"""
    shards = archive_service.list_shards(config)
    return {"total": len(shards), "items": shards}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条存储配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"存储配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条存储配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="存储配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条存储配置执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/archive", response_model=ActionResult)
def run_archive(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """执行归档：中断的批次从失败分片继续，同一路径的并发请求只放行先到的一个。"""
    batch, message, ok = archive_service.execute_archive(entry_id)
    return ActionResult(ok=ok, message=message, entry=batch)
