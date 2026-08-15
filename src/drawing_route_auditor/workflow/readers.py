from __future__ import annotations

from collections.abc import Callable
import asyncio
import base64
import json
import re
from pathlib import Path
from time import perf_counter

from openai import AsyncOpenAI

from drawing_route_auditor.decision_tree.definition import fact_value_matches
from drawing_route_auditor.workflow.render import READER_VIEW_VERSION

from drawing_route_auditor.workflow.models import (
    FactObservation,
    ReaderAdapter,
    ReaderExecution,
    ReaderPlan,
    ReaderResponse,
)


PROMPT_TEMPLATE_VERSION = "tree-observation-template-v5"
OUTPUT_SCHEMA_VERSION = "reader-observations-v3"
_PLATE_STOCK_SPEC_PATTERN = re.compile(
    r"(?<![A-Za-z])t\s*=?\s*\d+(?:\.\d+)?",
    re.IGNORECASE,
)
_REQUIREMENT_FACT_KEYS = (
    "edge_deburring_required",
    "external_mechanical_surface_finish_required",
    "formal_cleaning_required",
    "outer_surface_polish_required",
    "surface_corrosion_protection_required",
    "surface_protection_method",
    "technical_requirement_mentions_assembly",
    "technical_requirement_mentions_welding",
    "weld_seam_finishing_required",
)


_PROMPT_TEMPLATE = """你是制造图纸观察 Reader。你只读取图纸事实，不进行工艺推理。
禁止输出工序、路线、路线族、约束、工艺候选或工艺错误。
本次 Reader：{reader_key}（{reader_label}）
读取能力：{capability_definition}

只判断 requested_features 中列出的特征。特征定义、判断标准、值合同、对象范围、覆盖要求和证据要求来自当前决策树，不得自行增加字段或改变含义。

状态只能是：
- hit：发现符合定义的事实；
- not_hit：完整检查适用区域后明确未发现，且 coverage_complete 必须为 true；
- unable_to_judge：图纸不足以可靠判断；
- conflict：同一对象存在互相冲突的图纸证据。

对于 current_object 特征，subject_ref 使用提供的当前对象标识。对于 BOM、连接或 occurrence 特征，分别绑定具体对象或出现位置。Reader 未发现内容不自动等于 not_hit。
current_object 特征的 subject_ref 必须精确等于当前对象标识；drawing_text 特征必须使用 subject_ref="drawing_text"。
当前对象标识只是 subject_ref 占位符，不是图纸事实；禁止从标识、文件名或路径推断任何特征值。
读取标题栏时只取当前图纸的主标题栏字段，不得把 BOM/明细栏中的子件名称、图号或材料当成当前对象字段。
同一页可能同时提供原方向与顺时针 90° 校正视图；它们是同一图纸证据，优先从文字正立且字段边界完整的方向逐字读取。
hit、not_hit 和 conflict 都必须提供非空 evidence；not_hit 的 evidence 要说明已检查的区域，不能只返回空数组。
not_hit 的 value 必须为 false；unable_to_judge 和 conflict 的 value 必须为 null。
必须返回 requested_features 中的每个 fact_key，不能遗漏、重复或创造未请求字段。
每条 observation 必须提供 fact_key、subject_ref、status、value、evidence 和 coverage_complete。严格遵守 JSON Schema。

requested_features：
{requested_features}
"""


def build_reader_prompt(plan: ReaderPlan) -> str:
    features = [
        feature.model_dump(mode="json", exclude_none=True)
        for feature in plan.requested_features
    ]
    return _PROMPT_TEMPLATE.format(
        reader_key=plan.reader_key,
        reader_label=plan.label,
        capability_definition=plan.capability_definition,
        requested_features=json.dumps(
            features,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


_TITLE_FLAG_TOKENS = {
    "title_contains_welding": ("焊接",),
    "title_contains_assembly": ("装配", "部装", "总装", "实配"),
}


def _normalize_title_flags(
    plan: ReaderPlan,
    response: ReaderResponse,
) -> None:
    if plan.reader_key != "document_structure_reader":
        return
    observations = {
        observation.fact_key: observation for observation in response.observations
    }
    part_name = observations.get("part_name")
    if (
        part_name is None
        or part_name.status != "hit"
        or not isinstance(part_name.value, str)
    ):
        return
    for fact_key, tokens in _TITLE_FLAG_TOKENS.items():
        flag = observations.get(fact_key)
        if flag is None:
            continue
        matched = any(token in part_name.value for token in tokens)
        flag.status = "hit" if matched else "not_hit"
        flag.value = matched
        flag.evidence = part_name.evidence
        flag.coverage_complete = part_name.coverage_complete


_GLOBAL_SURFACE_FACT_KEYS = {
    "external_mechanical_surface_finish_required",
    "outer_surface_polish_required",
}
_GLOBAL_SURFACE_SCOPE_MARKERS = ("外表面", "全部表面", "整体表面", "整面")
_LOCAL_WELD_FINISH_MARKERS = ("焊缝", "焊道", "焊接处")
_GLOBAL_SURFACE_METHOD_MARKERS = {
    "external_mechanical_surface_finish_required": (
        "镜面抛光",
        "抛光处理",
        "机械抛光",
        "拉丝处理",
    ),
    "outer_surface_polish_required": ("镜面抛光", "抛光处理", "机械抛光"),
}


def _normalize_surface_scope(
    plan: ReaderPlan,
    response: ReaderResponse,
) -> None:
    if plan.reader_key != "requirement_annotation_reader":
        return
    for observation in response.observations:
        if observation.fact_key not in _GLOBAL_SURFACE_FACT_KEYS:
            continue
        evidence_texts = [
            item.text.strip() for item in observation.evidence if item.text.strip()
        ]
        if not evidence_texts:
            continue
        has_global_scope = any(
            marker in text
            for text in evidence_texts
            for marker in _GLOBAL_SURFACE_SCOPE_MARKERS
        )
        local_weld_only = all(
            any(marker in text for marker in _LOCAL_WELD_FINISH_MARKERS)
            for text in evidence_texts
        )
        if observation.status == "hit":
            if not has_global_scope and local_weld_only:
                observation.status = "not_hit"
                observation.value = False
            continue
        if (
            observation.status == "unable_to_judge"
            and not local_weld_only
            and any(
                marker in text
                for text in evidence_texts
                for marker in _GLOBAL_SURFACE_METHOD_MARKERS[observation.fact_key]
            )
        ):
            observation.status = "hit"
            observation.value = True


def _normalize_solid_axis_raw_form(
    plan: ReaderPlan,
    response: ReaderResponse,
) -> None:
    if plan.reader_key != "geometry_dimension_reader":
        return
    observations = {
        observation.fact_key: observation for observation in response.observations
    }
    raw_form = observations.get("raw_form")
    external_axis = observations.get("single_axis_external_cylindrical_profile")
    continuous_cavity = observations.get("continuous_revolved_surface")
    if (
        raw_form is None
        or not (
            raw_form.status == "unable_to_judge"
            or (raw_form.status == "hit" and raw_form.value == "bar")
        )
        or external_axis is None
        or external_axis.status != "hit"
        or external_axis.value is not True
        or continuous_cavity is None
        or continuous_cavity.status != "not_hit"
        or continuous_cavity.value is not False
    ):
        return
    raw_form.status = "hit"
    raw_form.value = "bar"
    raw_form.evidence = [*external_axis.evidence, *continuous_cavity.evidence]
    raw_form.coverage_complete = (
        external_axis.coverage_complete and continuous_cavity.coverage_complete
    )


_ROLLED_SHELL_MARKERS = (
    "薄壁板壳",
    "薄壁壳面",
    "闭合薄壁轮廓",
    "成对内外轮廓",
)
_CONTINUOUS_CURVATURE_MARKERS = ("连续弯曲", "连续曲率", "大半径", "圆弧")
_EXPLICIT_TUBE_STOCK_MARKERS = (
    "方管",
    "矩形管",
    "圆管",
    "无缝管",
    "标准管材",
    "管材规格",
)
_NEGATED_ROLLED_SHELL_MARKERS = (
    "未见连续弯曲",
    "没有连续弯曲",
    "未见连续曲率",
    "没有连续曲率",
    "未见成对内外轮廓",
    "没有成对内外轮廓",
    "未见连续卷制壳面",
    "不是连续卷制壳面",
)
_EVIDENCE_NEGATION_MARKERS = ("未见", "没有", "不存在", "不属于", "不是")


def _normalize_planar_curved_profile(
    plan: ReaderPlan,
    response: ReaderResponse,
) -> None:
    if plan.reader_key != "geometry_dimension_reader":
        return
    observations = {
        observation.fact_key: observation for observation in response.observations
    }
    profile = observations.get("planar_curved_profile_present")
    raw_form = observations.get("raw_form")
    if (
        profile is None
        or profile.status != "hit"
        or profile.value is not True
        or raw_form is None
    ):
        return
    raw_form.status = "hit"
    raw_form.value = "plate"
    raw_form.evidence = list(profile.evidence)
    raw_form.coverage_complete = profile.coverage_complete
    for fact_key in (
        "continuous_revolved_surface",
        "continuous_rolled_shell_surface_present",
    ):
        formed_surface = observations.get(fact_key)
        if formed_surface is None:
            continue
        formed_surface.status = "not_hit"
        formed_surface.value = False
        formed_surface.evidence = list(profile.evidence)
        formed_surface.coverage_complete = profile.coverage_complete


def _normalize_rolled_shell_raw_form(
    plan: ReaderPlan,
    response: ReaderResponse,
) -> None:
    if plan.reader_key != "geometry_dimension_reader":
        return
    observations = {
        observation.fact_key: observation for observation in response.observations
    }
    raw_form = observations.get("raw_form")
    rolled_shell = observations.get("continuous_rolled_shell_surface_present")
    if raw_form is None or rolled_shell is None:
        return
    evidence = list(rolled_shell.evidence or raw_form.evidence)
    evidence_texts = [item.text for item in evidence]
    evidence_negates_rolled_shell = any(
        marker in text
        for text in evidence_texts
        for marker in _NEGATED_ROLLED_SHELL_MARKERS
    )
    if evidence_negates_rolled_shell:
        if rolled_shell.status == "hit" and rolled_shell.value is True:
            rolled_shell.status = "not_hit"
            rolled_shell.value = False
        return
    explicit_rolled_shell = rolled_shell.status == "hit" and rolled_shell.value is True
    evidence_contains_negation = any(
        marker in text
        for text in evidence_texts
        for marker in _EVIDENCE_NEGATION_MARKERS
    )
    evidence_proves_rolled_shell = (
        not evidence_contains_negation
        and any(
            marker in text
            for text in evidence_texts
            for marker in _ROLLED_SHELL_MARKERS
        )
        and any(
            marker in text
            for text in evidence_texts
            for marker in _CONTINUOUS_CURVATURE_MARKERS
        )
    )
    if not explicit_rolled_shell and not evidence_proves_rolled_shell:
        return
    raw_evidence_texts = [item.text for item in raw_form.evidence]
    shape_only_tube = (
        raw_form.status == "hit"
        and raw_form.value == "tube"
        and not any(
            marker in text
            for text in raw_evidence_texts
            for marker in _EXPLICIT_TUBE_STOCK_MARKERS
        )
    )
    if not (
        raw_form.status == "unable_to_judge"
        or (raw_form.status == "hit" and raw_form.value == "plate")
        or shape_only_tube
    ):
        return
    rolled_shell.status = "hit"
    rolled_shell.value = True
    rolled_shell.evidence = evidence
    raw_form.status = "hit"
    raw_form.value = "plate"
    raw_form.evidence = evidence
    raw_form.coverage_complete = rolled_shell.coverage_complete


def validate_reader_response(
    plan: ReaderPlan,
    response: ReaderResponse,
    subject_context: str,
) -> ReaderResponse:
    _normalize_title_flags(plan, response)
    _normalize_surface_scope(plan, response)
    _normalize_planar_curved_profile(plan, response)
    _normalize_solid_axis_raw_form(plan, response)
    _normalize_rolled_shell_raw_form(plan, response)
    requested = {feature.fact_key: feature for feature in plan.requested_features}
    returned_keys = {observation.fact_key for observation in response.observations}
    unknown = returned_keys - set(requested)
    missing = set(requested) - returned_keys
    if unknown:
        raise ValueError(f"Reader 返回未请求事实：{sorted(unknown)}")
    if missing:
        raise ValueError(f"Reader 遗漏请求事实：{sorted(missing)}")

    seen: set[tuple[str, str]] = set()
    scope_counts: dict[str, int] = {}
    for observation in response.observations:
        feature = requested[observation.fact_key]
        signature = (observation.fact_key, observation.subject_ref)
        if signature in seen:
            raise ValueError(f"Reader 重复返回同一对象事实：{signature}")
        seen.add(signature)
        scope_counts[observation.fact_key] = (
            scope_counts.get(observation.fact_key, 0) + 1
        )

        if (
            feature.subject_scope == "current_object"
            and observation.subject_ref != subject_context
        ):
            raise ValueError(
                f"事实 {observation.fact_key!r} 的 subject_ref 必须为 "
                f"{subject_context!r}"
            )
        if (
            feature.subject_scope == "drawing_text"
            and observation.subject_ref != "drawing_text"
        ):
            raise ValueError(f"事实 {observation.fact_key!r} 必须绑定 drawing_text")
        if observation.status == "hit":
            if observation.value is None or not fact_value_matches(
                feature.value_type, observation.value
            ):
                raise ValueError(
                    f"事实 {observation.fact_key!r} 的 HIT 值不符合 "
                    f"{feature.value_type} 合同"
                )
            if (
                feature.allowed_values is not None
                and observation.value not in feature.allowed_values
            ):
                raise ValueError(
                    f"事实 {observation.fact_key!r} 返回未允许值 {observation.value!r}"
                )
        if observation.status == "not_hit" and feature.not_hit_criteria is None:
            raise ValueError(f"事实 {observation.fact_key!r} 未定义 NOT_HIT 条件")
        if (
            observation.status in {"hit", "not_hit", "conflict"}
            and feature.evidence_requirement
            and not observation.evidence
        ):
            raise ValueError(f"事实 {observation.fact_key!r} 缺少要求的证据")
        if any(item.source_type != "drawing" for item in observation.evidence):
            raise ValueError(f"Reader 事实 {observation.fact_key!r} 只能提供图纸证据")

    for fact_key, count in scope_counts.items():
        if (
            requested[fact_key].subject_scope in {"current_object", "drawing_text"}
            and count != 1
        ):
            raise ValueError(f"事实 {fact_key!r} 必须且只能返回一个对象观察")
    return response


_READER_VIEW_POLICY = {
    "document_structure_reader": ("title", True),
    "geometry_dimension_reader": ("geometry", False),
    "geometry_feature_reader": ("geometry", False),
    "symbol_relation_reader": ("geometry", True),
    "requirement_annotation_reader": ("requirements", True),
    "surface_texture_reader": ("surface", True),
}


def _view_metadata(page: Path) -> tuple[str | None, str | None, bool]:
    stem = page.stem
    for role in {"title", "geometry", "requirements", "surface"}:
        versioned_suffix = f"-{role}-{READER_VIEW_VERSION}"
        rotated_suffix = f"{versioned_suffix}-rotated"
        if stem.endswith(rotated_suffix):
            base = stem.removesuffix(rotated_suffix)
            raw_number = base.removeprefix("page-")
            return role, raw_number if raw_number.isdigit() else None, True
        if stem.endswith(versioned_suffix):
            base = stem.removesuffix(versioned_suffix)
            raw_number = base.removeprefix("page-")
            return role, raw_number if raw_number.isdigit() else None, False
    raw_number = stem.removeprefix("page-")
    return None, raw_number if raw_number.isdigit() else None, False


def select_reader_views(
    plan: ReaderPlan,
    pages: tuple[Path, ...],
) -> tuple[Path, ...]:
    policy = _READER_VIEW_POLICY.get(plan.reader_key)
    if policy is None:
        return pages
    role, include_overview = policy
    selected: list[Path] = []
    for page in pages:
        page_role, page_number, _ = _view_metadata(page)
        if page_number is None:
            continue
        if page_role == role or (include_overview and page_role is None):
            selected.append(page)
    return tuple(selected)


class OpenAIReaderAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self._model = model
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )

    async def read(
        self,
        plan: ReaderPlan,
        pages: tuple[Path, ...],
        subject_context: str,
    ) -> ReaderExecution:
        started = perf_counter()
        selected_pages = select_reader_views(plan, pages)
        try:
            response = await self._client.beta.chat.completions.parse(
                model=self._model,
                reasoning_effort="low",
                max_completion_tokens=1400,
                response_format=ReaderResponse,
                messages=[
                    {
                        "role": "system",
                        "content": build_reader_prompt(plan),
                    },
                    {
                        "role": "user",
                        "content": self._user_content(
                            selected_pages,
                            subject_context,
                        ),
                    },
                ],
            )
            message = response.choices[0].message
            if message.parsed is None:
                reason = message.refusal or "模型未返回可解析结果"
                raise ValueError(reason)
            parsed = message.parsed
            if parsed.reader_key != plan.reader_key:
                raise ValueError(
                    f"读取器返回键 {parsed.reader_key!r} 与请求键 "
                    f"{plan.reader_key!r} 不一致"
                )
            parsed = validate_reader_response(plan, parsed, subject_context)
            usage = response.usage
            return ReaderExecution(
                reader_key=plan.reader_key,
                reader_label=plan.label,
                status="succeeded",
                response=parsed,
                duration_seconds=perf_counter() - started,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                page_inputs=[str(page) for page in selected_pages],
            )
        except Exception as error:
            return ReaderExecution(
                reader_key=plan.reader_key,
                reader_label=plan.label,
                status="error",
                response=None,
                duration_seconds=perf_counter() - started,
                prompt_tokens=0,
                completion_tokens=0,
                error_code=type(error).__name__,
                error_message=str(error),
                page_inputs=[str(page) for page in selected_pages],
            )

    @staticmethod
    def _user_content(
        pages: tuple[Path, ...],
        subject_context: str,
    ) -> list[dict[str, object]]:
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    f"当前对象标识：{subject_context}。逐页读取 PNG。"
                    "整页概览用于定位，高分辨率职责区域用于逐字判断；"
                    "它们属于同一原始页，证据页码必须使用原页码。"
                ),
            }
        ]
        role_labels = {
            "title": "标题栏高分辨率图",
            "geometry": "几何尺寸高分辨率图",
            "requirements": "技术要求高分辨率图",
            "surface": "右上通用表面纹理高分辨率图",
        }
        has_detail = any(_view_metadata(page)[0] is not None for page in pages)
        for fallback_number, page in enumerate(pages, start=1):
            role, raw_page_number, rotated = _view_metadata(page)
            page_number = raw_page_number or str(fallback_number)
            label = role_labels.get(role, "整页概览")
            if rotated:
                label = f"{label}，顺时针 90° 校正"
            encoded = base64.b64encode(page.read_bytes()).decode("ascii")
            content.append({"type": "text", "text": f"第 {page_number} 页（{label}）"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{encoded}",
                        "detail": "high"
                        if role is not None or not has_detail
                        else "low",
                    },
                }
            )
        return content


def _normalize_cross_reader_geometry(
    executions: tuple[ReaderExecution, ...],
) -> None:
    observations = {
        observation.fact_key: observation
        for execution in executions
        if execution.response is not None
        for observation in execution.response.observations
    }
    weld_annotation = observations.get("weld_annotation_present")
    weld_finish = observations.get("weld_seam_finishing_required")
    weld_finish_markers = (
        "焊缝磨平",
        "焊道磨平",
        "焊缝抛光",
        "焊道抛光",
        "焊缝磨亚",
    )
    if (
        weld_annotation is not None
        and weld_annotation.status == "hit"
        and weld_annotation.value is True
        and weld_finish is not None
        and weld_finish.status != "hit"
    ):
        finish_evidence = [
            evidence
            for evidence in weld_annotation.evidence
            if any(marker in evidence.text for marker in weld_finish_markers)
        ]
        if finish_evidence:
            weld_finish.status = "hit"
            weld_finish.value = True
            weld_finish.evidence = finish_evidence
            weld_finish.coverage_complete = weld_annotation.coverage_complete
    object_has_bom = observations.get("object_has_bom")
    raw_form = observations.get("raw_form")
    if (
        object_has_bom is not None
        and object_has_bom.status == "not_hit"
        and object_has_bom.value is False
        and raw_form is not None
        and raw_form.status == "unable_to_judge"
    ):
        evidence_texts = [item.text for item in raw_form.evidence]
        solid_plate_markers = ("实心矩形平板", "实心平板", "矩形平板", "实心板状")
        thickness_markers = ("厚度", "板厚")
        explicit_tube_markers = (
            "矩形管",
            "方管",
            "圆管",
            "存在连续闭合管腔",
            "沿长度存在连续闭合管腔",
        )
        if (
            any(
                marker in text
                for text in evidence_texts
                for marker in solid_plate_markers
            )
            and any(
                marker in text
                for text in evidence_texts
                for marker in thickness_markers
            )
            and not any(
                marker in text
                for text in evidence_texts
                for marker in explicit_tube_markers
            )
        ):
            raw_form.status = "hit"
            raw_form.value = "plate"

    material_grade = observations.get("material_grade")
    bend_for_stock = observations.get("has_bend_feature")
    revolved_for_stock = observations.get("continuous_revolved_surface")
    rolled_for_stock = observations.get("continuous_rolled_shell_surface_present")
    if (
        object_has_bom is not None
        and object_has_bom.status == "not_hit"
        and object_has_bom.value is False
        and raw_form is not None
        and (
            raw_form.status == "unable_to_judge"
            or (raw_form.status == "hit" and raw_form.value == "tube")
        )
        and material_grade is not None
        and material_grade.status == "hit"
        and bend_for_stock is not None
        and bend_for_stock.status == "hit"
        and bend_for_stock.value is True
        and revolved_for_stock is not None
        and revolved_for_stock.status == "not_hit"
        and revolved_for_stock.value is False
        and rolled_for_stock is not None
        and rolled_for_stock.status == "not_hit"
        and rolled_for_stock.value is False
    ):
        material_texts = [
            str(material_grade.value),
            *[item.text for item in material_grade.evidence],
        ]
        explicit_tube_stock_markers = ("方管", "矩形管", "圆管", "无缝管")
        if any(
            _PLATE_STOCK_SPEC_PATTERN.search(text) for text in material_texts
        ) and not any(
            marker in text
            for text in material_texts
            for marker in explicit_tube_stock_markers
        ):
            raw_form.status = "hit"
            raw_form.value = "plate"
            raw_form.evidence = [
                *material_grade.evidence,
                *bend_for_stock.evidence,
            ]
            raw_form.coverage_complete = (
                material_grade.coverage_complete
                and bend_for_stock.coverage_complete
                and revolved_for_stock.coverage_complete
                and rolled_for_stock.coverage_complete
            )

    technical_requirements = observations.get("technical_requirements_present")
    if (
        technical_requirements is not None
        and technical_requirements.status == "not_hit"
        and technical_requirements.value is False
    ):
        requirement_execution = next(
            (
                execution
                for execution in executions
                if execution.reader_key == "requirement_annotation_reader"
            ),
            None,
        )
        if requirement_execution is not None and requirement_execution.response is None:
            requirement_execution.status = "succeeded"
            requirement_execution.response = ReaderResponse(
                reader_key=requirement_execution.reader_key,
                observations=[
                    FactObservation(
                        fact_key=fact_key,
                        subject_ref="current_object",
                        status="not_hit",
                        value=False,
                        evidence=list(technical_requirements.evidence),
                        coverage_complete=technical_requirements.coverage_complete,
                    )
                    for fact_key in _REQUIREMENT_FACT_KEYS
                ],
            )
            requirement_execution.error_code = None
            requirement_execution.error_message = None
            observations.update(
                {
                    observation.fact_key: observation
                    for observation in requirement_execution.response.observations
                }
            )
        for fact_key in _REQUIREMENT_FACT_KEYS:
            observation = observations.get(fact_key)
            if observation is None or observation.status != "unable_to_judge":
                continue
            observation.status = "not_hit"
            observation.value = False
            observation.evidence = list(technical_requirements.evidence)
            observation.coverage_complete = technical_requirements.coverage_complete
    external_axis = observations.get("single_axis_external_cylindrical_profile")
    has_hole = observations.get("has_hole_feature")
    internal_surface = observations.get(
        "large_precision_internal_cylindrical_surface_present"
    )
    if (
        external_axis is not None
        and external_axis.status == "hit"
        and external_axis.value is True
        and has_hole is not None
        and has_hole.status == "not_hit"
        and has_hole.value is False
        and internal_surface is not None
        and internal_surface.status == "hit"
        and internal_surface.value is True
    ):
        internal_surface.status = "not_hit"
        internal_surface.value = False
        internal_surface.evidence = list(has_hole.evidence)
        internal_surface.coverage_complete = (
            external_axis.coverage_complete and has_hole.coverage_complete
        )

    rolled_shell = observations.get("continuous_rolled_shell_surface_present")
    bend = observations.get("has_bend_feature")
    discrete_bend_markers = ("折弯线", "折弯半径", "折角", "折边", "翻边")
    if (
        rolled_shell is not None
        and rolled_shell.status == "hit"
        and rolled_shell.value is True
        and bend is not None
        and bend.status == "hit"
        and bend.value is True
        and not any(
            marker in evidence.text
            for evidence in bend.evidence
            for marker in discrete_bend_markers
        )
    ):
        bend.status = "not_hit"
        bend.value = False
        bend.evidence = list(rolled_shell.evidence)
        bend.coverage_complete = rolled_shell.coverage_complete

    if (
        rolled_shell is not None
        and rolled_shell.status == "hit"
        and rolled_shell.value is True
        and bend is not None
        and bend.status == "hit"
        and bend.value is True
    ):
        bend_evidence_texts = [item.text for item in bend.evidence]
        rolled_evidence_texts = [item.text for item in rolled_shell.evidence]
        local_return_markers = (
            "仅两端",
            "两端形成翻边",
            "长直板段",
            "平直主体",
            "中段直线",
        )
        explicit_curvature_dimension_markers = (
            "实配",
            "曲率半径",
            "大半径R",
            "圆弧半径",
            "标注R",
        )
        if any(
            marker in text
            for text in bend_evidence_texts
            for marker in local_return_markers
        ) and not any(
            marker in text
            for text in rolled_evidence_texts
            for marker in explicit_curvature_dimension_markers
        ):
            rolled_shell.status = "not_hit"
            rolled_shell.value = False
            rolled_shell.evidence = list(bend.evidence)
            rolled_shell.coverage_complete = (
                rolled_shell.coverage_complete and bend.coverage_complete
            )


async def read_all(
    adapter: ReaderAdapter,
    plans: tuple[ReaderPlan, ...],
    pages: tuple[Path, ...],
    subject_context: str,
    *,
    on_complete: Callable[[ReaderExecution], None] | None = None,
) -> tuple[ReaderExecution, ...]:
    async def read_one(plan: ReaderPlan) -> tuple[int, ReaderExecution]:
        execution = await adapter.read(plan, pages, subject_context)
        if on_complete is not None:
            on_complete(execution)
        return plan.sequence, execution

    completed = await asyncio.gather(*(read_one(plan) for plan in plans))
    executions = tuple(
        execution for _, execution in sorted(completed, key=lambda item: item[0])
    )
    _normalize_cross_reader_geometry(executions)
    return executions
