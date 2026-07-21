from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from drawing_route_auditor.db.connection import Connection
from drawing_route_auditor.decision_tree.definition import DecisionTreeDefinition
from drawing_route_auditor.decision_tree.importer import TreeUpdateSummary, persist_tree_revision


TreeCollection = Literal[
    "readers",
    "facts",
    "nodes",
    "branches",
    "rules",
    "edges",
]


class TreePatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["upsert", "remove"]
    collection: TreeCollection
    key: str = Field(min_length=1)
    value: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_value(self) -> TreePatchOperation:
        if self.op == "upsert" and self.value is None:
            raise ValueError("upsert 操作必须提供 value")
        if self.op == "remove" and self.value is not None:
            raise ValueError("remove 操作不能提供 value")
        return self


class DecisionTreePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    tree_key: str = Field(min_length=1)
    operations: list[TreePatchOperation] = Field(min_length=1)


_KEY_FIELDS: dict[TreeCollection, str] = {
    "readers": "reader_key",
    "facts": "fact_key",
    "nodes": "node_key",
    "branches": "branch_key",
    "rules": "rule_key",
    "edges": "",
}


def _edge_key(item: dict[str, object]) -> str:
    source = item.get("from_node_key") or item.get("from_branch_key") or "root"
    return (
        f"{item.get('edge_kind')}:{source}->{item.get('to_node_key')}:"
        f"{item.get('predecessor_ref')}"
    )


def _item_key(collection: TreeCollection, item: dict[str, object]) -> str:
    if collection == "edges":
        return _edge_key(item)
    key = item.get(_KEY_FIELDS[collection])
    if not isinstance(key, str) or not key:
        raise ValueError(f"{collection} 项缺少稳定键")
    return key


def _apply_operation(
    payload: dict[str, object],
    operation: TreePatchOperation,
) -> None:
    raw_collection = payload.get(operation.collection)
    if not isinstance(raw_collection, list):
        raise ValueError(f"当前决策树缺少集合 {operation.collection!r}")
    items: list[dict[str, object]] = []
    for raw in raw_collection:
        if not isinstance(raw, dict):
            raise ValueError(f"{operation.collection} 中存在非对象项")
        items.append(raw)

    matches = [
        index
        for index, item in enumerate(items)
        if _item_key(operation.collection, item) == operation.key
    ]
    if len(matches) > 1:
        raise ValueError(
            f"{operation.collection} 中稳定键 {operation.key!r} 不唯一"
        )

    if operation.op == "remove":
        if not matches:
            raise ValueError(
                f"无法删除不存在的 {operation.collection}:{operation.key}"
            )
        del items[matches[0]]
    else:
        if operation.value is None:
            raise RuntimeError("已校验的 upsert 操作缺少 value")
        value = deepcopy(operation.value)
        actual_key = _item_key(operation.collection, value)
        if actual_key != operation.key:
            raise ValueError(
                f"补丁键 {operation.key!r} 与 value 稳定键 {actual_key!r} 不一致"
            )
        if matches:
            items[matches[0]] = value
        else:
            items.append(value)

    payload[operation.collection] = items


def load_tree_patch(path: Path) -> DecisionTreePatch:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return DecisionTreePatch.model_validate(payload)


def apply_tree_patch(
    connection: Connection,
    patch_path: Path,
) -> TreeUpdateSummary:
    patch = load_tree_patch(patch_path)
    with connection.transaction():
        tree = connection.execute(
            """
            SELECT id
            FROM decision_trees
            WHERE tree_key = %s
            FOR UPDATE
            """,
            (patch.tree_key,),
        ).fetchone()
        if tree is None:
            raise LookupError(f"决策树 {patch.tree_key!r} 尚未初始化")
        current = connection.execute(
            """
            SELECT source_payload
            FROM decision_tree_versions
            WHERE tree_id = %s AND status = 'active'
            """,
            (tree["id"],),
        ).fetchone()
        if current is None:
            raise RuntimeError("当前决策树没有活动存储修订")
        payload = deepcopy(current["source_payload"])
        if not isinstance(payload, dict):
            raise RuntimeError("当前决策树存储载荷无效")

        for operation in patch.operations:
            _apply_operation(payload, operation)

        definition = DecisionTreeDefinition.model_validate(payload)
        return persist_tree_revision(
            connection,
            definition=definition,
            source_payload=payload,
            source_label=f"patch:{patch_path}",
        )
