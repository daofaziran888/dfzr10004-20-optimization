"""归档流程回归测试：以 STOR-0003 与 AUDI-0003 为锚点覆盖完整链路。

覆盖点：
- 按配置编号、存储路径、保留天数与分片边界选出待迁移分片；
- 中断后从失败分片继续，已确认分片不重复搬移；
- 归档完成前不清理源分片，校验失败保留原数据并生成待重试清单；
- 同一路径并发归档拒绝后到请求；
- 历史保留天数与已归档记录不被新配置改写；
- 批次、失败分片与容量回收状态持久化，重载后一致；
- 存储配置页与审计台账展示同一份容量回收结论。
"""
from __future__ import annotations

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 必须在导入应用之前指定独立数据文件，避免测试污染仓库里的运行数据
os.environ["APP_DATA_FILE"] = str(Path(tempfile.mkdtemp(prefix="archive-regression-")) / "store.json")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.storage import archive_service
from app.store import Store, store

client = TestClient(app)

STOR_ENABLED_ID = 3  # STOR-0003，已启用
STOR_DRAFT_ID = 1  # STOR-0001，草稿
SEED_AUDI_0003_SUMMARY = "采集审计日志样例3"


@pytest.fixture(autouse=True)
def fresh_store(tmp_path):
    store.reset(path=tmp_path / "store.json")
    archive_service.move_delay = 0.0
    yield
    store.reset(path=tmp_path / "store.json")


def run_archive(entry_id: int = STOR_ENABLED_ID) -> dict:
    response = client.post(f"/api/storage/{entry_id}/archive", json={"values": {}})
    assert response.status_code == 200
    return response.json()


def get_batch(code: str) -> dict:
    response = client.get(f"/api/storage/archives/{code}")
    assert response.status_code == 200
    return response.json()


def shard_states() -> dict[str, str]:
    response = client.get("/api/storage/shards", params={"config": "STOR-0003"})
    assert response.status_code == 200
    return {item["分片编号"]: item["status"] for item in response.json()["items"]}


def test_archive_selects_by_retention_and_records_progress():
    """首次归档：按保留天数与分片边界选片，记录已处理分片、失败文件与校验结果。"""
    result = run_archive()
    assert result["ok"] is True
    batch = result["entry"]

    # 保留天数 30 天：2026-07/08/09 分片到期，2026-10/11 仍在保留期内
    assert batch["选中分片"] == [
        "STOR-0003-2026-07",
        "STOR-0003-2026-08",
        "STOR-0003-2026-09",
    ]
    assert batch["保留天数"] == 30
    assert batch["存储路径"] == "/data/collect/warehouse"

    # 读写异常分片导致迁移中断：批次保持进行中，失败文件已记录
    assert batch["状态"] == "进行中"
    assert batch["已处理分片"] == ["STOR-0003-2026-07"]
    assert batch["失败分片"][0]["分片编号"] == "STOR-0003-2026-08"
    assert batch["失败分片"][0]["失败文件"], "失败分片必须记录失败文件"

    # 归档前校验结果逐分片留痕，校验异常分片保留原数据
    verify = {item["分片编号"]: item["结果"] for item in batch["校验结果"]}
    assert verify["STOR-0003-2026-07"] == "通过"
    assert verify["STOR-0003-2026-09"] == "失败"

    # 归档完成前不能清理源分片：已确认分片只到「已迁移」
    states = shard_states()
    assert states["STOR-0003-2026-07"] == "已迁移"
    assert states["STOR-0003-2026-08"] == "在线"
    assert states["STOR-0003-2026-09"] == "在线"
    assert states["STOR-0003-2026-10"] == "在线"


def test_resume_continues_from_failed_shard_without_duplicating():
    """中断续跑：从失败分片继续，已确认分片不重复搬移，完成后才清理源分片。"""
    first = run_archive()["entry"]
    resumed = run_archive()
    assert resumed["ok"] is True
    batch = resumed["entry"]

    # 同一批次续跑，不新建批次
    assert batch["批次编号"] == first["批次编号"]
    # 已确认分片不重复搬移：07 只出现一次，08 本次补搬
    assert batch["已处理分片"] == ["STOR-0003-2026-07", "STOR-0003-2026-08"]
    assert batch["释放容量MB"] == 512  # 256 + 256，不多不少

    # 校验失败分片保留原数据，批次部分完成
    assert batch["状态"] == "部分完成"
    states = shard_states()
    assert states["STOR-0003-2026-07"] == "已清理"
    assert states["STOR-0003-2026-08"] == "已清理"
    assert states["STOR-0003-2026-09"] == "在线", "校验失败分片必须保留原数据"

    # 容量回收结论：1024 总量，在线占用 09(128)+10(256)+11(128)
    assert batch["剩余容量MB"] == 512

    # 审计台账同步：同一批次一条记录，容量回收结论一致
    audits = client.get("/api/audit", params={"size": 100}).json()["items"]
    synced = [a for a in audits if a.get("归档批次") == batch["批次编号"]]
    assert len(synced) == 1
    record = synced[0]
    assert record["记录编号"] == batch["审计记录编号"]
    assert "回收容量512MB" in record["结果摘要"]
    assert "STOR-0003-2026-09" in record["待重试分片"]
    assert record["历史摘要"], "续跑同步必须保留原摘要"

    # 待重试清单挂在审计台账上；续跑成功的 08 不再待重试
    retries = client.get("/api/audit/pending-retries").json()["items"]
    retry_shards = {item["分片编号"] for item in retries}
    assert "STOR-0003-2026-09" in retry_shards
    assert "STOR-0003-2026-08" not in retry_shards
    assert "STOR-0003-2026-08" not in record["待重试分片"]

    # AUDI-0003 是历史已归档记录，不能被归档流程改写
    audi3 = client.get("/api/audit/3").json()
    assert audi3["记录编号"] == "AUDI-0003"
    assert audi3["status"] == "已归档"
    assert audi3["结果摘要"] == SEED_AUDI_0003_SUMMARY


def test_repeat_archive_never_moves_confirmed_shard_twice():
    """同一分片重复提交归档只生效一次：已确认分片不会进入新批次。"""
    run_archive()
    run_archive()  # 续跑到部分完成
    third = run_archive()
    # 校验失败分片仍在线且到期，会被新批次重试；已确认分片绝不再搬
    batches = client.get("/api/storage/archives").json()["items"]
    confirmed: list[str] = []
    for batch in batches:
        confirmed += batch["已处理分片"]
    assert sorted(confirmed) == ["STOR-0003-2026-07", "STOR-0003-2026-08"]
    assert len(confirmed) == len(set(confirmed)), "同一分片不能被重复搬移"

    summary = client.get("/api/storage/archives/summary").json()
    stor3 = next(c for c in summary["configs"] if c["配置编号"] == "STOR-0003")
    assert stor3["已回收容量MB"] == 512, "重复提交不能重复回收容量"
    if third["ok"]:
        assert third["entry"]["已处理分片"] == []


def test_concurrent_archive_on_same_path_is_rejected():
    """同一路径并发归档：先到的执行，后到的拒绝并说明原因。"""
    archive_service.move_delay = 0.3
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: archive_service.execute_archive(STOR_ENABLED_ID), range(2)))
    finally:
        archive_service.move_delay = 0.0
    oks = sorted(ok for _, _, ok in outcomes)
    assert oks == [False, True]
    rejected_message = next(msg for _, msg, ok in outcomes if not ok)
    assert "正在执行" in rejected_message and "拒绝" in rejected_message

    batches = client.get("/api/storage/archives").json()["items"]
    assert len(batches) == 1, "并发提交只能生效一个批次"


def test_archive_requires_enabled_config_and_running_batch_blocks_disable():
    """未启用配置不能归档；批次进行中不能停用配置。"""
    rejected = run_archive(STOR_DRAFT_ID)
    assert rejected["ok"] is False
    assert "已启用" in rejected["message"]

    run_archive()  # STOR-0003 中断在「进行中」
    response = client.post(
        f"/api/storage/{STOR_ENABLED_ID}/actions", json={"values": {"action": "停用配置"}}
    )
    assert response.json()["ok"] is False
    assert "进行中" in response.json()["message"]


def test_new_config_cannot_rewrite_history():
    """历史保留天数与已归档记录不能被新配置改写。"""
    run_archive()
    run_archive()
    batch_code = client.get("/api/storage/archives").json()["items"][0]["批次编号"]

    response = client.post(
        "/api/storage",
        json={"values": {"配置编号": "STOR-0003", "存储路径": "/data/collect/warehouse", "文件格式": "parquet", "保留天数": 1}},
    )
    assert response.json()["ok"] is False
    assert "已存在" in response.json()["message"]

    batch = get_batch(batch_code)
    assert batch["保留天数"] == 30, "批次快照的保留天数不能被新配置改写"

    audi3 = client.get("/api/audit/3").json()
    assert audi3["status"] == "已归档"
    assert audi3["结果摘要"] == SEED_AUDI_0003_SUMMARY


def test_archive_state_persists_across_reload():
    """批次、失败分片与容量回收状态持久保存：重载数据文件后完全一致。"""
    first = run_archive()["entry"]
    reloaded = Store(path=store.path)

    batches = reloaded.rows("archive_batch")
    assert len(batches) == 1
    persisted = batches[0]
    assert persisted["批次编号"] == first["批次编号"]
    assert persisted["状态"] == "进行中"
    assert persisted["已处理分片"] == ["STOR-0003-2026-07"]
    assert persisted["失败分片"][0]["分片编号"] == "STOR-0003-2026-08"
    assert persisted["失败分片"][0]["失败文件"]

    shards = {s["分片编号"]: s["status"] for s in reloaded.rows("storage_shard")}
    assert shards["STOR-0003-2026-07"] == "已迁移"

    audits = reloaded.rows("audit")
    synced = [a for a in audits if a.get("归档批次") == first["批次编号"]]
    assert len(synced) == 1


def test_storage_page_and_audit_share_same_capacity_conclusion():
    """存储配置页与审计台账展示同一份容量回收结论。"""
    run_archive()
    run_archive()

    summary = client.get("/api/storage/archives/summary").json()
    stor3 = next(c for c in summary["configs"] if c["配置编号"] == "STOR-0003")
    assert stor3["已回收容量MB"] == 512
    assert stor3["剩余容量MB"] == 512
    assert "已回收512MB" in stor3["容量回收结论"]

    audits = client.get("/api/audit", params={"size": 100}).json()["items"]
    synced = next(a for a in audits if a.get("归档批次") == stor3["最近批次"])
    assert "回收容量512MB" in synced["结果摘要"]
    assert "剩余容量512MB" in synced["结果摘要"]
