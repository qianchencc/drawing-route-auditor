from __future__ import annotations

from dataclasses import dataclass

from drawing_route_auditor.workflow.models import (
    DrawingCase,
    FactObservation,
    FlowIssue,
    FlowResult,
    ReaderOperation,
)


VISION_FLOWS = (
    "blanking",
    "forming",
    "connection",
    "machining",
    "surface_cleaning",
)


@dataclass(frozen=True, slots=True)
class DispatchPlan:
    object_kind: str
    ownership_mode: str
    enabled_vision_flows: tuple[str, ...]
    skipped_vision_flows: tuple[str, ...]
    reasons: dict[str, str]


def plan_dispatch(case: DrawingCase) -> DispatchPlan:
    material_type = case.material_type.strip()
    pass_through = "虚拟" in material_type
    component_markers = ("结构件", "组焊", "装配件", "部件")
    object_kind = (
        "component"
        if any(marker in material_type for marker in component_markers)
        else "part"
    )

    if pass_through:
        enabled: tuple[str, ...] = ()
        reasons = {"all": "PLM 物料类型标记为虚拟件，本级不承担制造"}
    elif object_kind == "component":
        enabled_list = ["connection", "surface_cleaning"]
        if "精加工" in material_type:
            enabled_list.append("machining")
        enabled = tuple(enabled_list)
        reasons = {
            "connection": "部件层级需要检查本级连接或装配责任",
            "surface_cleaning": "部件技术要求可能产生表面或清洁工序",
        }
    else:
        enabled_list = ["surface_cleaning"]
        reasons = {
            "surface_cleaning": "零件技术要求需要独立检查表面与清洁责任"
        }
        if any(
            marker in material_type
            for marker in ("下料", "折弯", "钣金", "精加工", "备库")
        ):
            enabled_list.insert(0, "blanking")
            reasons["blanking"] = "PLM 物料类型表明本级从原材料制造"
        if any(marker in material_type for marker in ("折弯", "成形")):
            enabled_list.extend(("forming", "connection"))
            reasons["forming"] = "PLM 物料类型表明存在成形责任"
            reasons["connection"] = "闭合成形件需要并行检查拼板或接缝连接责任"
        elif "焊接" in material_type:
            enabled_list.append("connection")
            reasons["connection"] = "PLM 物料类型表明本级可能承担连接"
        if "精加工" in material_type:
            enabled_list.append("machining")
            reasons["machining"] = "PLM 物料类型表明本级承担精加工"
        enabled = tuple(dict.fromkeys(enabled_list))

    skipped = tuple(flow_id for flow_id in VISION_FLOWS if flow_id not in enabled)
    return DispatchPlan(
        object_kind=object_kind,
        ownership_mode="pass_through" if pass_through else "manufacture_here",
        enabled_vision_flows=enabled,
        skipped_vision_flows=skipped,
        reasons=reasons,
    )


def infer_transfer(case: DrawingCase) -> FlowResult:
    parent_type = (case.parent_part_type or "").strip()
    observation = FactObservation(
        fact_key="parent_part_type",
        subject_ref=case.parent_material_code or case.parent_drawing_no or "parent",
        status="hit" if parent_type else "unable_to_judge",
        value=parent_type or None,
        evidence=[],
        coverage_complete=bool(parent_type),
    )

    if "焊接" in parent_type:
        process = "转焊接"
    elif "部装" in parent_type:
        process = "转部装"
    elif any(marker in parent_type for marker in ("装配", "总装")):
        process = "转装配"
    else:
        issue = FlowIssue(
            kind="error",
            code="PARENT_PART_TYPE_UNDETERMINED",
            message="缺少可映射的上级制造类型，无法确定转序",
            affected_operation_keys=[],
            missing_facts=["parent_part_type"],
            candidate_options=[],
        )
        return FlowResult(
            flow_id="transfer",
            status="partial",
            observations=[observation],
            operations=[],
            constraints=[],
            issues=[issue],
        )

    operation = ReaderOperation(
        operation_key="to_parent",
        process=process,
        content=f"转入上级 {case.parent_drawing_no or ''} {case.parent_name or ''}".strip(),
        targets=[case.parent_material_code or case.parent_drawing_no or "parent"],
        necessity_status="confirmed_required",
        execution_state="ready",
        blocked_by=[],
    )
    return FlowResult(
        flow_id="transfer",
        status="complete",
        observations=[observation],
        operations=[operation],
        constraints=[],
        issues=[],
    )
