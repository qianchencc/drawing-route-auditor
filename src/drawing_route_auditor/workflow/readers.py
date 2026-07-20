from __future__ import annotations

from collections.abc import Callable
import asyncio
import base64
import json
from pathlib import Path
from time import perf_counter

from openai import AsyncOpenAI

from drawing_route_auditor.workflow.models import (
    ReaderAdapter,
    ReaderExecution,
    ReaderPlan,
    ReaderResponse,
)


PROMPT_TEMPLATE_VERSION = "tree-observation-template-v2"
OUTPUT_SCHEMA_VERSION = "reader-observations-v1"


_PROMPT_TEMPLATE = """你是制造图纸观察 Reader。你只读取图纸事实，不进行工艺推理。
禁止输出工序、路线、路线族、约束、工艺候选或工艺错误。
本次 Reader：{reader_key}（{reader_label}）
读取能力：{capability_definition}

只判断 requested_features 中列出的特征。特征定义、判断标准、值合同、对象范围、覆盖要求和证据要求来自当前决策树版本，不得自行增加字段或改变含义。

状态只能是：
- hit：发现符合定义的事实；
- not_hit：完整检查适用区域后明确未发现，且 coverage_complete 必须为 true；
- unable_to_judge：图纸不足以可靠判断；
- conflict：同一对象存在互相冲突的图纸证据。

对于 current_object 特征，subject_ref 使用提供的当前对象标识。对于 BOM、连接或 occurrence 特征，分别绑定具体对象或出现位置。Reader 未发现内容不自动等于 not_hit。
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
                            pages,
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
            usage = response.usage
            return ReaderExecution(
                reader_key=plan.reader_key,
                reader_label=plan.label,
                status="succeeded",
                response=parsed,
                duration_seconds=perf_counter() - started,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
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
                    "同一页可能同时提供低分辨率整页概览和高分辨率图框放大图；"
                    "两者属于同一页，必须合并判断，证据页码使用原页码。"
                ),
            }
        ]
        focus_page_stems = {
            page.stem.removesuffix("-focus")
            for page in pages
            if page.stem.endswith("-focus")
        }
        for fallback_number, page in enumerate(pages, start=1):
            is_focus = page.stem.endswith("-focus")
            base_stem = page.stem.removesuffix("-focus")
            raw_page_number = base_stem.removeprefix("page-")
            page_number = (
                raw_page_number if raw_page_number.isdigit() else str(fallback_number)
            )
            label = "图框放大图" if is_focus else "整页概览"
            encoded = base64.b64encode(page.read_bytes()).decode("ascii")
            content.append({"type": "text", "text": f"第 {page_number} 页（{label}）"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{encoded}",
                        "detail": (
                            "high"
                            if is_focus or base_stem not in focus_page_stems
                            else "low"
                        ),
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
