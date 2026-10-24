"""SecondaryMailbox（二等信箱）：来源开放、仅驻内存的版本化临时信息。

id（标识）、version（版本）、source（来源）、kind（类型）、payload（数据）、
scope（作用域）、coalesce_key（可选合并键）、metadata（仅本地的生产者适配数据）。
"""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable
from uuid import uuid4


class SecondaryMailbox:
    """唯一待消费存储；生产者负责业务校验和正文去重。"""

    def __init__(self):
        self._items: dict[str, dict] = {}
        self._keys: dict[tuple, str] = {}
        self._scopes: set[str] = set()
        self.version = 0  # version（最近登记版本），清理后仍保持递增。
        self._consumers: list[Callable] = []
        self._default_scope = self.new_scope()

    def new_scope(self) -> str:
        """建立 scope（有效作用域）；消费不使冻结快照失效。"""
        scope = uuid4().hex
        self._scopes.add(scope)
        return scope

    def scope_valid(self, scope: str) -> bool:
        return scope in self._scopes

    def has_scope(self, scope: str) -> bool:
        """查询作用域内是否待消费，不复制其他生产者正文。"""
        return any(row["scope"] == scope for row in self._items.values())

    def on_consume(self, callback: Callable) -> None:
        """登记生产者消费回执，不按来源分支处理业务。"""
        self._consumers.append(callback)

    def publish(self, *, source: str, kind: str, payload: dict, scope: str | None = None,
                coalesce_key: str | None = None, metadata: dict | None = None) -> dict:
        """发布通用条目；metadata（本地适配数据）不投喂给模型。"""
        if not isinstance(source, str) or not source or not isinstance(kind, str) or not kind:
            raise ValueError("二等信息的 source（来源）和 kind（类型）必须为非空字符串")
        if not isinstance(payload, dict) or (metadata is not None and not isinstance(metadata, dict)):
            raise ValueError("二等信息 payload（数据）和 metadata（本地适配数据）必须为对象")
        if coalesce_key is not None and not isinstance(coalesce_key, str):
            raise ValueError("coalesce_key（合并键）必须为字符串")
        scope = self._default_scope if scope is None else scope
        if not self.scope_valid(scope):
            raise ValueError("二等信息作用域已失效")
        payload = deepcopy(payload)
        metadata = deepcopy(metadata or {})
        key = (scope, source, kind, coalesce_key) if coalesce_key is not None else None
        if key is not None:
            self.remove(self._keys.get(key))
        self.version += 1
        item = {"id": uuid4().hex, "version": self.version, "source": source,
                "kind": kind, "payload": payload, "scope": scope,
                "coalesce_key": coalesce_key, "metadata": metadata}
        self._items[item["id"]] = item
        if key is not None:
            self._keys[key] = item["id"]
        return deepcopy(item)

    def snapshot(self) -> dict[str, dict]:
        return deepcopy(self._items)

    def valid(self, captured: dict[str, dict] | None) -> dict[str, dict]:
        """只检查作用域；已确认消费的原请求仍可以重试。"""
        return {key: deepcopy(row) for key, row in (captured or {}).items()
                if isinstance(row, dict) and row.get("id") == key
                and self.scope_valid(row.get("scope"))}

    def remove(self, item_id: str | None) -> bool:
        row = self._items.pop(item_id, None)
        if row is None:
            return False
        if row["coalesce_key"] is not None:
            key = (row["scope"], row["source"], row["kind"], row["coalesce_key"])
            if self._keys.get(key) == item_id:
                del self._keys[key]
        return True

    def consume(self, captured: dict[str, dict]) -> list[str]:
        """批量确认捕获版本；调用方在整个批次结束后处理空置信号。"""
        valid = self.valid(captured)
        consumed = []
        for key, row in valid.items():
            current = self._items.get(key)
            if current is not None and current["version"] == row["version"]:
                self.remove(key)
                consumed.append(key)
        for callback in self._consumers:
            callback(valid)
        return consumed

    def restore(self, captured: dict[str, dict]) -> list[str]:
        """混合评估过期后归还仍有效的冻结项；不覆盖更新版本，不新增注入次数。"""
        restored = []
        for item_id, row in self.valid(captured).items():
            if item_id in self._items:
                continue
            key = ((row["scope"], row["source"], row["kind"], row["coalesce_key"])
                   if row["coalesce_key"] is not None else None)
            if key is not None and key in self._keys:
                continue
            self._items[item_id] = row
            if key is not None:
                self._keys[key] = item_id
            restored.append(item_id)
        return restored

    def invalidate(self, scope: str) -> int:
        self._scopes.discard(scope)
        removed = [key for key, row in self._items.items() if row["scope"] == scope]
        for key in removed:
            self.remove(key)
        return len(removed)

    def clear(self) -> None:
        self._items.clear()
        self._keys.clear()
        self._scopes.clear()
        self._default_scope = self.new_scope()

    def __bool__(self):
        return bool(self._items)

    @staticmethod
    def project(captured: dict[str, dict]) -> list[dict]:
        """模型仅读取 source（来源）、kind（类型）、payload（数据）。"""
        return [{key: deepcopy(row[key]) for key in ("source", "kind", "payload")}
                for row in captured.values()]
