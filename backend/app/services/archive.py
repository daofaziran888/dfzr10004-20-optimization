"""采集存储归档引擎：从存储配置到审计台账的完整归档链路。

执行口径：
- 按配置编号、存储路径、保留天数与分片边界（分片起始/截止）选出待迁移分片；
- 归档前先校验，校验失败的分片保留原数据，并进入审计台账的待重试清单；
- 迁移中断后批次保持「进行中」，再次提交从失败分片继续，已确认分片不重复搬移；
- 批次完成前源分片只标记「已迁移」，完成（或部分完成）后才清理为「已清理」；
- 同一存储路径的并发归档：后到请求直接拒绝，由调用方稍后重试；
- 批次落盘持久化，容量回收结论同时提供给存储配置页与采集审计台账。
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

CONFIG_MODULE = "storage"
SHARD_MODULE = "storage_shard"
BATCH_MODULE = "archive_batch"
AUDIT_MODULE = "audit"

BATCH_RUNNING = "进行中"
BATCH_DONE = "已完成"
BATCH_PARTIAL = "部分完成"

SHARD_ONLINE = "在线"
SHARD_MOVED = "已迁移"
SHARD_CLEANED = "已清理"

# 批次内分片状态：待校验 -> 待搬移 -> 已确认；失败 / 校验失败 可随批次续跑重试
SHARD_STATE_PENDING_VERIFY = "待校验"
SHARD_STATE_PENDING_MOVE = "待搬移"
SHARD_STATE_CONFIRMED = "已确认"
SHARD_STATE_FAILED = "失败"
SHARD_STATE_VERIFY_FAILED = "校验失败"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> date:
    return date.today()


class ArchiveService:
    """存储归档的执行与查询都收在这里，路由层只做参数传递。"""

    def __init__(self) -> None:
        self._path_locks: dict[str, threading.Lock] = {}
        self._path_locks_guard = threading.Lock()
        # 测试注入项：模拟单分片迁移耗时，用来覆盖同路径并发归档的场景
        self.move_delay = 0.0

    # ------------------------------------------------------------------ 查询
    def list_batches(self, config_code: str | None = None) -> list[dict[str, Any]]:
        batches = store.rows(BATCH_MODULE)
        if config_code:
            batches = [b for b in batches if b.get("配置编号") == config_code]
        return sorted(batches, key=lambda b: int(b.get("id", 0)))

    def get_batch(self, batch_code: str) -> dict[str, Any] | None:
        for batch in store.rows(BATCH_MODULE):
            if batch.get("批次编号") == batch_code:
                return self._enrich_batch(batch)
        return None

    def list_shards(self, config_code: str | None = None) -> list[dict[str, Any]]:
        shards = store.rows(SHARD_MODULE)
        if config_code:
            shards = [s for s in shards if s.get("配置编号") == config_code]
        return sorted(shards, key=lambda s: str(s.get("分片编号", "")))

    def capacity_summary(self) -> dict[str, Any]:
        """容量回收结论：存储配置页与采集审计台账共用这一份口径。"""
        configs: list[dict[str, Any]] = []
        total_reclaimed = 0
        total_remaining = 0
        for config in store.rows(CONFIG_MODULE):
            code = str(config.get("配置编号", ""))
            capacity = int(config.get("路径容量MB") or 0)
            shards = [s for s in store.rows(SHARD_MODULE) if s.get("配置编号") == code]
            reclaimed = sum(int(s.get("大小MB") or 0) for s in shards if s.get("status") == SHARD_CLEANED)
            occupied = sum(
                int(s.get("大小MB") or 0) for s in shards if s.get("status") in (SHARD_ONLINE, SHARD_MOVED)
            )
            remaining = capacity - occupied
            batches = [b for b in store.rows(BATCH_MODULE) if b.get("配置编号") == code]
            conclusion = f"已回收{reclaimed}MB，剩余容量{remaining}MB/{capacity}MB"
            configs.append({
                "配置编号": code,
                "存储路径": config.get("存储路径"),
                "路径容量MB": capacity,
                "已回收容量MB": reclaimed,
                "剩余容量MB": remaining,
                "归档批次数": len(batches),
                "最近批次": batches[-1].get("批次编号") if batches else None,
                "容量回收结论": conclusion,
            })
            total_reclaimed += reclaimed
            total_remaining += remaining
        return {
            "configs": configs,
            "totals": {
                "已回收容量MB": total_reclaimed,
                "剩余容量MB": total_remaining,
                "归档批次数": len(store.rows(BATCH_MODULE)),
            },
        }

    def pending_retries(self) -> list[dict[str, Any]]:
        """采集审计台账的待重试清单：当前仍处于失败或校验失败状态的分片。

        批次里的「失败分片」是历史留痕；续跑成功后分片状态转为已确认，
        就不再出现在待重试清单里。
        """
        items: list[dict[str, Any]] = []
        for batch in store.rows(BATCH_MODULE):
            audit_code = batch.get("审计记录编号")
            failures = {f.get("分片编号"): f for f in batch.get("失败分片", [])}
            for code, state in batch.get("分片状态", {}).items():
                if state == SHARD_STATE_FAILED:
                    failure = failures.get(code, {})
                    items.append({
                        "归档批次": batch.get("批次编号"),
                        "配置编号": batch.get("配置编号"),
                        "分片编号": code,
                        "原因": failure.get("原因", "迁移失败"),
                        "失败文件": failure.get("失败文件", []),
                        "审计记录编号": audit_code,
                        "状态": "待重试",
                    })
                elif state == SHARD_STATE_VERIFY_FAILED:
                    items.append({
                        "归档批次": batch.get("批次编号"),
                        "配置编号": batch.get("配置编号"),
                        "分片编号": code,
                        "原因": "归档前校验失败，源数据已保留",
                        "失败文件": [],
                        "审计记录编号": audit_code,
                        "状态": "待重试",
                    })
        return items

    # ------------------------------------------------------------------ 执行
    def execute_archive(self, entry_id: int) -> tuple[dict[str, Any] | None, str, bool]:
        """执行（或续跑）一条存储配置的归档；返回批次、说明与是否生效。"""
        config = store.find(CONFIG_MODULE, entry_id)
        if config is None:
            return None, f"存储配置 {entry_id} 不存在或已归档", False
        if config.get("status") != "已启用":
            return None, f"存储配置 {config.get('配置编号')} 当前状态为「{config.get('status')}」，仅已启用配置可执行归档", False

        path = str(config.get("存储路径") or "")
        lock = self._lock_for(path)
        if not lock.acquire(blocking=False):
            running = self._running_batch(path)
            hint = f"，进行中批次 {running.get('批次编号')}" if running else ""
            return None, f"存储路径 {path} 已有归档任务正在执行{hint}，后到请求已拒绝，请稍后重试", False
        try:
            batch = self._running_batch_for_config(str(config.get("配置编号")))
            if batch is not None:
                message = f"归档批次 {batch.get('批次编号')} 从失败分片继续执行"
            else:
                shards = self._eligible_shards(config)
                if not shards:
                    return None, f"存储配置 {config.get('配置编号')} 没有超过保留天数的在线分片，无需归档", False
                batch = self._create_batch(config, shards)
                message = f"归档批次 {batch.get('批次编号')} 已创建，选中 {len(shards)} 个分片"

            self._run_batch(batch, config)
            store.save()
            state = batch.get("状态")
            if state == BATCH_RUNNING:
                failed = "、".join(f.get("分片编号", "") for f in batch.get("失败分片", []))
                message += f"；迁移在分片 {failed} 处中断，已记录失败文件，可再次提交继续"
            else:
                message += f"；批次{state}，回收容量{batch.get('释放容量MB')}MB，剩余容量{batch.get('剩余容量MB')}MB"
            return self._enrich_batch(batch), message, True
        finally:
            lock.release()

    # ---------------------------------------------------------------- 内部步骤
    def _lock_for(self, path: str) -> threading.Lock:
        with self._path_locks_guard:
            return self._path_locks.setdefault(path, threading.Lock())

    def _running_batch(self, path: str) -> dict[str, Any] | None:
        for batch in store.rows(BATCH_MODULE):
            if batch.get("存储路径") == path and batch.get("状态") == BATCH_RUNNING:
                return batch
        return None

    def _running_batch_for_config(self, config_code: str) -> dict[str, Any] | None:
        for batch in store.rows(BATCH_MODULE):
            if batch.get("配置编号") == config_code and batch.get("状态") == BATCH_RUNNING:
                return batch
        return None

    def _eligible_shards(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        """按配置编号、存储路径、保留天数与分片边界选出待迁移分片。

        已确认归档的分片（状态已流转）与任何批次里已确认的分片都不会再被选中，
        保证同一分片重复提交归档只生效一次。
        """
        retention_days = int(config.get("保留天数") or 0)
        cutoff = _today() - timedelta(days=retention_days)
        confirmed = {
            code
            for batch in store.rows(BATCH_MODULE)
            for code in batch.get("已处理分片", [])
        }
        eligible: list[dict[str, Any]] = []
        for shard in store.rows(SHARD_MODULE):
            if shard.get("配置编号") != config.get("配置编号"):
                continue
            if shard.get("存储路径") != config.get("存储路径"):
                continue
            if shard.get("status") != SHARD_ONLINE:
                continue
            if shard.get("分片编号") in confirmed:
                continue
            boundary_end = date.fromisoformat(str(shard.get("分片截止")))
            if boundary_end >= cutoff:
                continue
            eligible.append(shard)
        return sorted(eligible, key=lambda s: str(s.get("分片起始", "")))

    def _create_batch(self, config: dict[str, Any], shards: list[dict[str, Any]]) -> dict[str, Any]:
        batches = store.rows(BATCH_MODULE)
        batch_id = max((int(b.get("id", 0)) for b in batches), default=0) + 1
        selected = [str(s.get("分片编号")) for s in shards]
        batch = {
            "id": batch_id,
            "批次编号": f"ARCH-{batch_id:04d}",
            "配置编号": config.get("配置编号"),
            "存储路径": config.get("存储路径"),
            # 保留天数在批次创建时快照，之后新配置或配置变更都改不了历史批次
            "保留天数": int(config.get("保留天数") or 0),
            "路径容量MB": int(config.get("路径容量MB") or 0),
            "状态": BATCH_RUNNING,
            "status": BATCH_RUNNING,
            "pending": True,
            "abnormal": False,
            "选中分片": selected,
            "分片状态": {code: SHARD_STATE_PENDING_VERIFY for code in selected},
            "已处理分片": [],
            "失败分片": [],
            "校验结果": [],
            "释放容量MB": 0,
            "剩余容量MB": self._remaining_capacity(config),
            "创建时间": _now(),
            "完成时间": None,
            "审计记录编号": None,
        }
        batches.append(batch)
        return batch

    def _run_batch(self, batch: dict[str, Any], config: dict[str, Any]) -> None:
        shards = {str(s.get("分片编号")): s for s in store.rows(SHARD_MODULE)}
        states: dict[str, str] = batch["分片状态"]

        # 归档前校验：逐分片核对，校验失败的保留原数据并记入待重试清单
        for code in batch["选中分片"]:
            if states.get(code) != SHARD_STATE_PENDING_VERIFY:
                continue
            shard = shards.get(code)
            verified = shard is not None and shard.get("健康状态") != "校验异常"
            states[code] = SHARD_STATE_PENDING_MOVE if verified else SHARD_STATE_VERIFY_FAILED
            self._record_verify_result(batch, code, "通过" if verified else "失败")

        # 逐分片迁移：失败即中断，已确认的分片不再重复搬移
        for code in batch["选中分片"]:
            if states.get(code) not in (SHARD_STATE_PENDING_MOVE, SHARD_STATE_FAILED):
                continue
            shard = shards.get(code)
            if shard is None:
                states[code] = SHARD_STATE_FAILED
                self._record_failure(batch, code, "源分片不存在", [])
                break
            if shard.get("健康状态") == "读写异常" and not shard.get("已重试"):
                # 模拟瞬时 IO 故障：首次迁移失败并中断，再次提交从该分片继续
                shard["已重试"] = True
                states[code] = SHARD_STATE_FAILED
                failed_files = [f"{code}/part-{i:04d}" for i in range(int(shard.get("文件数") or 1))]
                self._record_failure(batch, code, "分片读写异常，迁移中断", failed_files)
                break
            if self.move_delay > 0:
                time.sleep(self.move_delay)
            shard["status"] = SHARD_MOVED
            shard["归档批次"] = batch.get("批次编号")
            states[code] = SHARD_STATE_CONFIRMED
            if code not in batch["已处理分片"]:
                batch["已处理分片"].append(code)

        unfinished = (SHARD_STATE_PENDING_VERIFY, SHARD_STATE_PENDING_MOVE, SHARD_STATE_FAILED)
        if any(states.get(code) in unfinished for code in batch["选中分片"]):
            batch["状态"] = BATCH_RUNNING
            batch["status"] = BATCH_RUNNING
            batch["pending"] = True
            self._sync_audit(batch, config)
            return
        self._finalize(batch, config, shards)

    def _finalize(self, batch: dict[str, Any], config: dict[str, Any], shards: dict[str, dict[str, Any]]) -> None:
        """批次收尾：确认过的源分片才允许清理，随后固化容量回收结论。"""
        states: dict[str, str] = batch["分片状态"]
        confirmed = [code for code in batch["选中分片"] if states.get(code) == SHARD_STATE_CONFIRMED]
        for code in confirmed:
            shard = shards.get(code)
            if shard is not None and shard.get("status") == SHARD_MOVED:
                shard["status"] = SHARD_CLEANED
        batch["释放容量MB"] = sum(
            int(shards[code].get("大小MB") or 0) for code in confirmed if code in shards
        )
        batch["剩余容量MB"] = self._remaining_capacity(config)
        has_verify_failure = any(states.get(code) == SHARD_STATE_VERIFY_FAILED for code in batch["选中分片"])
        batch["状态"] = BATCH_PARTIAL if has_verify_failure else BATCH_DONE
        batch["status"] = batch["状态"]
        batch["pending"] = False
        batch["abnormal"] = has_verify_failure
        batch["完成时间"] = _now()
        self._sync_audit(batch, config)

    def _remaining_capacity(self, config: dict[str, Any]) -> int:
        capacity = int(config.get("路径容量MB") or 0)
        occupied = sum(
            int(s.get("大小MB") or 0)
            for s in store.rows(SHARD_MODULE)
            if s.get("配置编号") == config.get("配置编号") and s.get("status") in (SHARD_ONLINE, SHARD_MOVED)
        )
        return capacity - occupied

    def _record_verify_result(self, batch: dict[str, Any], code: str, result: str) -> None:
        results = batch.setdefault("校验结果", [])
        for item in results:
            if item.get("分片编号") == code:
                item["结果"] = result
                return
        results.append({"分片编号": code, "结果": result})

    def _record_failure(self, batch: dict[str, Any], code: str, reason: str, files: list[str]) -> None:
        failures = batch.setdefault("失败分片", [])
        for item in failures:
            if item.get("分片编号") == code:
                item["原因"] = reason
                item["失败文件"] = files
                return
        failures.append({"分片编号": code, "原因": reason, "失败文件": files})

    # ---------------------------------------------------------------- 审计联动
    def _sync_audit(self, batch: dict[str, Any], config: dict[str, Any]) -> None:
        """把容量回收结论同步到采集审计台账；同一批次只挂一条审计记录。

        再次同步时保留原摘要到「历史摘要」，待重试分片随批次状态刷新。
        """
        audits = store.rows(AUDIT_MODULE)
        record = next((a for a in audits if a.get("归档批次") == batch.get("批次编号")), None)
        conclusion = (
            f"归档批次{batch.get('批次编号')}（{batch.get('状态')}）："
            f"已处理分片{len(batch.get('已处理分片', []))}个，"
            f"回收容量{batch.get('释放容量MB', 0)}MB，"
            f"路径剩余容量{batch.get('剩余容量MB')}MB"
        )
        retry_shards = [
            code
            for code, state in batch.get("分片状态", {}).items()
            if state in (SHARD_STATE_FAILED, SHARD_STATE_VERIFY_FAILED)
        ]
        detail = "；".join(
            f"{code} {batch.get('分片状态', {}).get(code)}" for code in batch.get("选中分片", [])
        )
        if record is None:
            record = {
                "id": max((int(a.get("id", 0)) for a in audits), default=0) + 1,
                "记录编号": self._next_audit_code(audits),
                "操作类型": "存储归档",
                "采集计划": config.get("配置编号"),
                "执行人": "system",
                "执行时间": _today().isoformat(),
                "结果摘要": conclusion,
                "变更内容": f"存储路径{batch.get('存储路径')} 分片明细：{detail}",
                "复核状态": "待复核",
                "归档批次": batch.get("批次编号"),
                "待重试分片": retry_shards,
                "历史摘要": [],
                "status": "已记录",
                "pending": True,
                "abnormal": bool(retry_shards),
            }
            audits.append(record)
        else:
            if record.get("结果摘要") != conclusion:
                record.setdefault("历史摘要", []).append(record.get("结果摘要"))
            record["结果摘要"] = conclusion
            record["变更内容"] = f"存储路径{batch.get('存储路径')} 分片明细：{detail}"
            record["待重试分片"] = retry_shards
            record["abnormal"] = bool(retry_shards)
        batch["审计记录编号"] = record.get("记录编号")

    @staticmethod
    def _next_audit_code(audits: list[dict[str, Any]]) -> str:
        seq = 0
        for row in audits:
            code = str(row.get("记录编号", ""))
            if code.startswith("AUDI-") and code[5:].isdigit():
                seq = max(seq, int(code[5:]))
        return f"AUDI-{seq + 1:04d}"

    def _enrich_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        """批次详情：补上每个选中分片的源分片状态、校验结果与失败文件。"""
        shards = {str(s.get("分片编号")): s for s in store.rows(SHARD_MODULE)}
        verify = {item.get("分片编号"): item.get("结果") for item in batch.get("校验结果", [])}
        failures = {item.get("分片编号"): item for item in batch.get("失败分片", [])}
        detail: list[dict[str, Any]] = []
        for code in batch.get("选中分片", []):
            shard = shards.get(code, {})
            failure = failures.get(code, {})
            detail.append({
                "分片编号": code,
                "大小MB": shard.get("大小MB"),
                "批次内状态": batch.get("分片状态", {}).get(code),
                "校验结果": verify.get(code, "未校验"),
                "源分片状态": shard.get("status", "缺失"),
                "失败文件": failure.get("失败文件", []),
            })
        enriched = dict(batch)
        enriched["分片明细"] = detail
        return enriched
