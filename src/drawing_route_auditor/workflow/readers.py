from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from time import perf_counter

from openai import AsyncOpenAI

from drawing_route_auditor.workflow.models import (
    DrawingCase,
    EvidenceRef,
    FactObservation,
    FlowIssue,
    FlowResult,
    FlowReader,
    ReaderExecution,
)


_BASE_PROMPT = """你是制造图纸的一个业务流程 Reader 与局部推理器。
禁止读取、猜测或复述历史工艺路线。只处理当前流程负责的制造决策；不要产出其他流程的工序。
先输出绑定 subject_ref 的图纸事实，再由这些事实产生本流程工序。
当前对象的 subject_ref 必须等于用户消息中的物料编码。
共享字段使用固定 fact_key：材料牌号=material_grade，板厚毫米数=sheet_thickness_mm；看不清时仍使用固定 key 并返回 unable_to_judge。
表面粗糙度符号旁的 12.5、6.3 等数值不是板厚，不得写入 sheet_thickness_mm。
事实状态只能是 hit、not_hit、unable_to_judge、conflict；NOT_HIT 必须完整检查适用区域且 coverage_complete=true。
图纸不足以支持企业认可的完整分支时输出 ERROR，不得自动生成“做/不做”候选。
CANDIDATES 只能用于有限、完整、互斥且每项有明确成立条件的企业分支。
只有确定需要的工序才能标为 confirmed_required；尚有前置决策时必须 blocked 或 conditional。
before_operation 表示较早工序，after_operation 表示较晚工序；不确定顺序时不要输出约束。
每项事实最多保留一条最直接证据；最多 8 项事实、4 项工序。严格遵守 JSON Schema。"""


_FLOW_PROMPTS: dict[str, str] = {
    "blanking": """当前流程：毛坯与下料。
只判断本级是否从原材料制造、原始形态、材料、厚度、展开/开料需求及下料方式。
本流程其他固定 fact_key：raw_form、requires_unfolding、contour_kind、requires_thick_plate_thermal_cutting。
允许的路线级工序包括激光下料、剪板下料、割板、数控等离子下料、数控火焰下料、锯床下料、铸造、锻造、特殊毛坯线切割。
粗糙度符号旁的 12.5、6.3 等数值不是板厚；板厚必须来自厚度尺寸、标题栏或材料规格证据。
当前经验规则：薄板非矩形展开轮廓且不需要厚板热切割时采用激光下料；简单直边矩形仍需设备规则。
无法在多个设备工艺间安全选择时返回候选或错误，不得按常识静默挑选。""",
    "forming": """当前流程：成形。
只判断卷圆、折弯、刨槽、冲压、翻边、校形等以改变形状为主的工序。
本流程固定 fact_key：forming_geometry_kind、closed_shell、has_explicit_bend_lines、requires_initial_roll、requires_post_weld_rounding；同时复核 material_grade、sheet_thickness_mm。
光滑连续的圆锥、圆筒或圆弧壳体且无折弯线时属于卷圆，不得因为剖面存在斜线就输出折弯。
大型闭合薄板壳体要区分初次卷圆和焊后校圆；只有图纸证据或已验证工艺模块支持时才输出第二道卷圆。
区分图纸明确要求、几何必需和仅可能需要。校形没有企业规则或明确依据时不得提升为确定工序。""",
    "connection": """当前流程：连接与装配。
只判断当前层级承担的焊接（校正）、装配、部装、实配、铆接、粘接或永久固定。
本流程固定 fact_key：current_level_owns_connection、weld_symbol_present、preforming_plate_splice_required、closing_seam_weld_required；同时复核 sheet_thickness_mm。
焊接符号必须绑定到具体对象、连接或出现位置；不能因为看到焊接外观就假设当前层级承担焊接。
对于由板材形成的闭合壳体，分别检查成形前拼板焊接和成形后闭合接缝焊接；证据不足时不得自动输出两道焊接。""",
    "machining": """当前流程：精加工。
只判断车、粗车、精车、铣、镗、钻孔、攻丝、磨削、线切割等去除材料或保证尺寸的工序。
必须分别判断是否需要加工、加工阶段和加工方法。普通尺寸或未注公差本身不能证明需要精加工。""",
    "surface_cleaning": """当前流程：表面与清洁。
只判断抛光、拉丝、镜面、去毛刺、焊缝磨平、清洗、擦拭及其他表面处理。
本流程固定 fact_key：edge_deburring_required、outer_surface_polish_required、formal_cleaning_required、surface_stage_owner。
锐边倒钝如果只能作为其他工序的工艺内容，不得擅自提升为独立路线工序。
若当前对象是将转入焊接父件的子件，必须判断表面要求由当前层级还是父件最终阶段承担；无法确定阶段时输出 SURFACE_STAGE_UNDETERMINED，不得把要求直接提升为当前工序。
清洗必须有图纸要求、污染后果和企业清洁策略；不能由抛光无条件推出。""",
}

_ALLOWED_PROCESSES: dict[str, frozenset[str]] = {
    "blanking": frozenset({
        "激光下料", "剪板下料", "割板", "数控等离子下料",
        "数控火焰下料", "锯床下料", "铸造", "锻造", "线切割",
    }),
    "forming": frozenset({"卷圆", "折弯", "刨槽", "冲压", "翻边", "校形"}),
    "connection": frozenset({
        "焊接(校正)", "装配", "部装", "实配", "铆接", "粘接",
    }),
    "machining": frozenset({
        "车", "粗车", "精车", "铣", "镗", "钻孔", "攻丝", "磨", "线切割",
    }),
    "surface_cleaning": frozenset({
        "抛光", "拉丝", "镜面", "去毛刺", "去角", "清洗", "擦拭",
    }),
}


class OpenAIFlowReader:
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
        flow_id: str,
        pages: tuple[Path, ...],
        case: DrawingCase,
    ) -> ReaderExecution:
        if flow_id not in _FLOW_PROMPTS:
            raise ValueError(f"Unknown reader flow: {flow_id}")

        started = perf_counter()
        try:
            response = await self._client.beta.chat.completions.parse(
                model=self._model,
                reasoning_effort="minimal",
                max_completion_tokens=900,
                response_format=FlowResult,
                messages=[
                    {
                        "role": "system",
                        "content": f"{_BASE_PROMPT}\n\n{_FLOW_PROMPTS[flow_id]}",
                    },
                    {
                        "role": "user",
                        "content": self._user_content(flow_id, pages, case),
                    },
                ],
            )
            message = response.choices[0].message
            if message.parsed is None:
                reason = message.refusal or "model returned no parsed result"
                raise ValueError(reason)
            result = message.parsed
            if result.flow_id != flow_id:
                raise ValueError(
                    f"reader returned flow_id={result.flow_id!r}; expected {flow_id!r}"
                )
            result = self._normalize_result(result)
            usage = response.usage
            return ReaderExecution(
                flow_result=result,
                duration_seconds=perf_counter() - started,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
            )
        except Exception as error:
            return ReaderExecution(
                flow_result=self._local_failure(flow_id, case, error),
                duration_seconds=perf_counter() - started,
                prompt_tokens=0,
                completion_tokens=0,
            )

    @staticmethod
    def _user_content(
        flow_id: str,
        pages: tuple[Path, ...],
        case: DrawingCase,
    ) -> list[dict[str, object]]:
        context = {
            "material_code": case.material_code,
            "drawing_no": case.drawing_no,
            "part_name": case.part_name,
            "material_type": case.material_type,
            "parent_drawing_no": case.parent_drawing_no,
            "parent_name": case.parent_name,
            "parent_part_type": case.parent_part_type,
        }
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    f"流程={flow_id}。以下是允许使用的 PLM 上下文：{context!r}。"
                    "PLM 上下文不是历史路线。逐页读取 PNG，只返回本流程结果。"
                ),
            }
        ]
        for page_number, page in enumerate(pages, start=1):
            encoded = base64.b64encode(page.read_bytes()).decode("ascii")
            content.append({"type": "text", "text": f"第 {page_number} 页"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{encoded}",
                        "detail": "high",
                    },
                }
            )
        return content

    @staticmethod
    def _normalize_result(result: FlowResult) -> FlowResult:
        issues = list(result.issues)
        allowed = _ALLOWED_PROCESSES[result.flow_id]
        operations = []
        for operation in result.operations:
            if operation.process in allowed:
                operations.append(operation)
                continue
            issues.append(
                FlowIssue(
                    kind="error",
                    code="NON_OPERATION_PLACEHOLDER_REMOVED",
                    message=(
                        f"{operation.process!r} 不是 {result.flow_id} 流程的标准工序；"
                        "已从路线中移除"
                    ),
                    affected_operation_keys=[],
                    missing_facts=[],
                    candidate_options=[],
                )
            )

        unresolved = [
            observation.fact_key
            for observation in result.observations
            if observation.status in {
                "unable_to_judge", "conflict", "missing_due_to_reader_failure"
            }
        ]
        already_reported = {
            fact_key for issue in issues for fact_key in issue.missing_facts
        }
        unreported = [key for key in unresolved if key not in already_reported]
        if unreported:
            issues.append(
                FlowIssue(
                    kind="error",
                    code="UNRESOLVED_READER_FACT",
                    message="Reader 存在未决事实，已限制在当前流程",
                    affected_operation_keys=[],
                    missing_facts=unreported,
                    candidate_options=[],
                )
            )

        status = result.status
        if issues or any(
            operation.execution_state != "ready" for operation in operations
        ):
            status = "partial" if status != "error" else status
        return result.model_copy(
            update={"status": status, "operations": operations, "issues": issues}
        )

    @staticmethod
    def _local_failure(
        flow_id: str,
        case: DrawingCase,
        error: Exception,
    ) -> FlowResult:
        message = f"{type(error).__name__}: {error}"
        observation = FactObservation(
            fact_key=f"{flow_id}_reader_output",
            subject_ref=case.material_code,
            status="missing_due_to_reader_failure",
            value=None,
            evidence=[],
            coverage_complete=False,
        )
        issue = FlowIssue(
            kind="error",
            code="LOCAL_READER_FAILURE",
            message=message,
            affected_operation_keys=[],
            missing_facts=[observation.fact_key],
            candidate_options=[],
        )
        return FlowResult(
            flow_id=flow_id,
            status="error",
            observations=[observation],
            operations=[],
            constraints=[],
            issues=[issue],
        )


async def read_flows(
    reader: FlowReader,
    flow_ids: tuple[str, ...],
    pages: tuple[Path, ...],
    case: DrawingCase,
) -> tuple[ReaderExecution, ...]:
    executions = await asyncio.gather(
        *(reader.read(flow_id, pages, case) for flow_id in flow_ids)
    )
    return tuple(executions)
