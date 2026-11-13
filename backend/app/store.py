"""数据仓库：给每个业务模块准备一份可筛选、可流转的数据，并持久化到本地 JSON 文件。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
归档批次、失败分片与容量回收状态都落在数据文件里，服务重启、页面刷新后仍然一致。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from app.seed import SEED_ROWS

DEFAULT_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "store.json"


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = Path(path or os.environ.get("APP_DATA_FILE") or DEFAULT_DATA_FILE)
        self._tables: dict[str, list[dict[str, Any]]] = {}
        if not self._load():
            self.reset(save=True)

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> bool:
        """从数据文件恢复全部业务表；文件不存在或损坏时返回 False。"""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return False
        try:
            tables = json.loads(raw)
        except json.JSONDecodeError:
            return False
        if not isinstance(tables, dict):
            return False
        with self._lock:
            self._tables = {
                str(name): [dict(row) for row in rows]
                for name, rows in tables.items()
                if isinstance(rows, list)
            }
            for name, rows in SEED_ROWS.items():
                self._tables.setdefault(name, [dict(row) for row in rows])
        return True

    def save(self) -> None:
        """把当前数据原子写入数据文件，避免写一半留下损坏文件。"""
        with self._lock:
            payload = json.dumps(self._tables, ensure_ascii=False, indent=1)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=self._path.name, suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(tmp_name, self._path)
            except OSError:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

    def reset(self, path: str | Path | None = None, *, save: bool = True) -> None:
        """恢复成示例数据；测试用它拿到一份干净的仓库，并可选切换数据文件。"""
        if path is not None:
            self._path = Path(path)
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }
        if save:
            self.save()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

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


store = Store()
