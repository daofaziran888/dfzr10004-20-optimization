"""采集存储策略业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "storage"
BATCH_MODULE = "archive_batch"
REQUIRED_FIELDS = ["配置编号", "存储路径", "文件格式"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class StorageService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        code = str(values.get("配置编号") or "").strip()
        # 配置编号一旦登记就不允许再用新配置覆盖：历史保留天数与已归档记录不能被改写
        if any(str(row.get("配置编号", "")) == code for row in rows):
            return None, [f"配置编号 {code} 已存在，新配置不能改写历史保留天数与已归档记录"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("分片大小", "压缩算法", "保留天数", "归档策略", "是否启用", "路径容量MB"):
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        store.save()
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"存储配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集存储策略可执行范围"
        if action == "停用配置":
            running = next(
                (
                    batch
                    for batch in store.rows(BATCH_MODULE)
                    if batch.get("配置编号") == entry.get("配置编号") and batch.get("状态") == "进行中"
                ),
                None,
            )
            if running is not None:
                return None, f"归档批次 {running.get('批次编号')} 仍在进行中，完成前不能停用配置"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save()
        return entry, f"存储配置已{action}"
