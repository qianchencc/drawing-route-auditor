from __future__ import annotations

from collections.abc import Callable
import asyncio
import base64
import json
from pathlib import Path
from time import perf_counter

from openai import AsyncOpenAI

from drawing_route_auditor.decision_tree.definition import fact_value_matches
from drawing_route_auditor.workflow.render import READER_VIEW_VERSION

from drawing_route_auditor.workflow.models import (
    ReaderAdapter,
    ReaderExecution,
    ReaderPlan,
    ReaderResponse,
)


PROMPT_TEMPLATE_VERSION = "tree-observation-template-v4"
OUTPUT_SCHEMA_VERSION = "reader-observations-v3"


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
    features = [feature.model_dump(mode="json") for feature in plan.requested_features]
    return _PROMPT_TEMPLATE.format(
        reader_key=plan.reader_key,
        reader_label=plan.label,
        capability_definition=plan.capability_definition,
        requested_features=json.dumps(features, ensure_ascii=False, indent=2),
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


def validate_reader_response(
    plan: ReaderPlan,
    response: ReaderResponse,
    subject_context: str,
) -> ReaderResponse:
    _normalize_title_flags(plan, response)
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


_READER_DETAIL_ROLE = {
    "document_structure_reader": "title",
    "geometry_dimension_reader": "geometry",
    "symbol_relation_reader": "geometry",
    "requirement_annotation_reader": "requirements",
}


def _view_metadata(page: Path) -> tuple[str | None, str | None, bool]:
    stem = page.stem
    for role in {"title", "geometry", "requirements"}:
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
    role = _READER_DETAIL_ROLE.get(plan.reader_key)
    if role is None:
        return pages
    include_overview = plan.reader_key != "geometry_dimension_reader"
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
                reasoning_effort="minimal",
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
    return tuple(
        execution for _, execution in sorted(completed, key=lambda item: item[0])
    )
