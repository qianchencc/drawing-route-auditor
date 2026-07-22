# 津南图纸到工艺路线

`drawing-route-auditor` 是一个面向制造图纸的异步工艺路线原型：输入 PDF 图纸，由多个视觉 Reader 并发提取可审计事实，再由 PostgreSQL 中的当前决策树生成一条确定路线、有限候选路线，或带局部问题的非完整结果。

当前实现是开发评估系统，不是可直接回写 PLM/ERP 的生产系统。路线状态不是准确率声明；只有通过案例门禁的规则才可继续扩展。

## 核心约束

- Reader 只读取图纸事实，不直接生成工序或路线。
- 每个事实保存状态、对象、覆盖情况和 PDF 页码、区域、原文证据。
- Reader 合同和决策规则统一来自 PostgreSQL 中的当前决策树。
- 信息不足时返回候选或局部错误，不猜测唯一答案。
- 表面、清洁或转序等局部工序不能单独冒充完整路线。
- 历史 CSV 路线只用于推荐完成后的开发评估，禁止进入推理上下文。
- 模型调用期间不持有 PostgreSQL 事务或行锁。

## 当前工作流

```text
PDF
→ Poppler 渲染页面并生成 Reader 视图
→ 并发运行 4 个图纸 Reader
→ 合并观察事实并定位冲突
→ 执行当前决策树和事实闭包
→ 组装确定路线或候选路线
→ 校验基础路线完整性
→ 持久化运行、事实、决策、工序和证据
→ 可选：后置加载历史 CSV 进行开发评估
```

当前决策树定义四类 Reader：

1. 文档结构：标题栏、图号、名称、材料、数量和 BOM；
2. 几何尺寸：形态、尺寸、板厚、孔槽、折弯和加工特征；
3. 符号关系：焊接符号、焊缝和装配连接关系；
4. 技术要求：粗糙度、公差、表面、清洁、检验及文字标注。

## 环境要求

- Python 3.13+
- PostgreSQL 17；仓库提供 `compose.yaml`
- Poppler，且 `pdftoppm` 位于 `PATH`
- 一个兼容 OpenAI API 的视觉模型端点

## 初始化

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

至少配置：

```dotenv
OPENAI_BASE_URL=https://example.invalid/v1
OPENAI_API_KEY=replace-me
MODEL=vision-model-name
```

启动并迁移数据库：

```bash
docker compose up -d postgres
.venv/bin/draw-route db wait
.venv/bin/draw-route db migrate
```

`db migrate` 同时顺序应用数据库结构和知识迁移。知识迁移在当前树存在时通过 copy-on-write 生成新修订，空数据库则保持空操作；首次部署仍由 `tree init` 写入 `docs/decision_tree.json`。已应用迁移不可改写，后续调整必须追加新的版本号，并保持重复执行幂等。

首次初始化决策树并校验：

```bash
.venv/bin/draw-route tree init docs/decision_tree.json
.venv/bin/draw-route tree validate
```

最后检查运行环境：

```bash
.venv/bin/draw-route doctor
```

## 决策树维护

运行时始终使用 PostgreSQL 中的当前树，不接受版本选择。初始化后通过增量补丁维护：

```bash
.venv/bin/draw-route tree list
.venv/bin/draw-route tree show
.venv/bin/draw-route tree export > current-tree.json
.venv/bin/draw-route tree validate
.venv/bin/draw-route tree evaluate --facts facts.json
.venv/bin/draw-route tree apply tree-patch.json

```

增量补丁只描述要新增、修改或删除的 `readers`、`facts`、`nodes`、`branches`、`rules` 或 `edges` 项，不需要再次提交整棵树。前五类分别使用 `reader_key`、`fact_key`、`node_key`、`branch_key`、`rule_key`；边使用 `edge_kind:来源->目标:predecessor_ref` 作为稳定键。例如：

`tree export` 从 PostgreSQL 无损导出当前规范载荷，可用于审查、生成下一份补丁和灾备；导出文件不是新的运行时主数据。`tree show --format json` 仍是面向人的中文展示投影。

```json
{
  "schema_version": 1,
  "tree_key": "drawing-process-tree",
  "operations": [
    {
      "op": "upsert",
      "collection": "rules",
      "key": "rolling_operation",
      "value": {
        "branch_key": "3.2",
        "rule_key": "rolling_operation",
        "description": "卷制路线族生成初次卷圆工序",
        "decision_key": "forming_operation",
        "question": "需要哪种成形工序？",
        "option_key": "rolling",
        "option_label": "卷圆",
        "result_kind": "resolved",
        "outcome_type": "process",
        "outcome_key": "forming_rolling",
        "outcome_value": {
          "operation_key": "forming",
          "process_name": "卷圆",
          "order_rank": 30
        },
        "clauses": [
          {
            "fact_key": "route_family",
            "operator": "eq",
            "expected_value": "rolled_sheet_part"
          }
        ]
      }
    }
  ]
}
```

这里“增量”和“原子”描述不同层面：维护接口是增量的；提交实现会锁定当前树、在内存中应用全部补丁、校验规则条件与规则产出的事实引用和值合同，并执行“四个 Reader、每个 Reader 至少负责一个观察事实”的运行时合同校验，只有全部成功才一次提交。每次路线运行在 Reader 启动前固定内部修订，后续事实闭包始终使用同一修订。数据库内部使用 copy-on-write 修订固定可执行决策和历史运行证据，但业务上始终只有一棵当前树，也不提供版本选择、启用或回退接口。

## 生成路线

默认表格输出带实时阶段进度、Reader 结果、逐工序决策、事实依据和来源证据：

```bash
.venv/bin/draw-route route /path/to/drawing.pdf
```

机器可读输出：

```bash
.venv/bin/draw-route route /path/to/drawing.pdf --format json
```


要求路线必须完整，否则返回非零退出码：

```bash
.venv/bin/draw-route route /path/to/drawing.pdf --require-complete
```

开发评估同样只接受 PDF；历史路线在推荐和全部推理证据持久化后，才按 PDF 文件名读取：

```bash
.venv/bin/draw-route route /path/to/DEMO-PLATE-001.pdf --evaluate
```

默认表格输出也会在推荐完成后按 PDF 文件名查找 `docs/routes_1.csv` 和 `docs/routes_2.csv`，仅追加显示参考路线。参考路线不进入 Reader、事实闭包或决策树上下文；JSON 输出在顶层 `参考路线` 返回相同的后置数据。

## 结果状态

| 状态 | 含义 |
|---|---|
| `complete` | 唯一基础路线完整，且没有未解决的局部问题 |
| `complete_with_candidates` | 基础路线完整，但存在有限、可枚举的企业允许分支 |
| `partial` | 仅确认部分安全工序；基础路线或前置事实尚未闭合 |
| `error` | 无法形成可用路线或命中完整性守卫 |

每个工序直接携带：

- 触发规则和决策问题；
- 已选项及其他候选项；
- 关键事实的中文名称、状态和值；
- PDF 页码、区域和逐字原文证据；
- 规则键、决策问题、规则说明和只读内部修订号。

每条候选都会打印完整工序序列；终端在全部路线之后集中打印一次候选差异和仍需事实。

## 数据与运行产物

- PostgreSQL：当前决策树、Reader 请求、PDF 事实观察、规则命中、路线候选、逐工序决策和开发评估；
- `.runtime/rendered/`：按 PDF 哈希和 DPI 缓存的渲染页，以及带版本号的职责区域和旋转校正视图；
- `docs/cases/`：隔离的开发案例上下文、历史答案和人工审查记录；这些文件不进入运行时推理。

`.runtime/` 是可再生缓存，不应作为知识源提交。

## 验证

```bash
ruff check src tests
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest
```

集成测试使用 `DRA_DATABASE_URL` 指向的 PostgreSQL。测试会迁移数据库，但不会调用真实视觉模型。

## 文档索引

- `docs/README.md`：文档入口、权威层级、系统数据流和维护流程；
- `docs/iteration_protocol.md`：当前单图门禁、运行编号、并发边界和结果语义；
- `docs/decision_tree.json`：空数据库初始化快照，也是当前 PostgreSQL 树的审查导出；
- `docs/base_research.md`：原始研究与长期目标，不代表当前已实现行为；
- `docs/drawing_process_tree_original.json`：客户原始决策树只读档案，不参与初始化、补丁或运行；
- `docs/cases/`：开发评估上下文、历史答案和人工审查记录，不进入推理；
- `docs/*.csv`：历史主数据和路线评估数据，只允许在推荐持久化后读取。

## 已知边界

- `route` 的推理输入只有 PDF；CLI 不提供物料编码、外部事实、PLM 或案例上下文注入接口。
- 决策条件只使用 PDF 中可观察的材料形态、全局几何、尺寸公差、孔槽拓扑、BOM 结构、焊接符号和技术要求。图号、名称和文件名只作审计元数据，禁止选择路线族或工序。
- 当前树覆盖板材下料与折弯、受完整性守卫约束的卷制、标准管材定长锯切、轴类棒料加工，以及焊接部件的首道连接、焊缝修平和大型精密内圆加工。
- 当前不推断最终转序。PDF 无法证明表面工序承担层级或后续制造阶段时，结果保持 `partial`，不得读取上级图号补齐。
- 当前不生成可直接下发的材料定额、设备、班组和工时。
- 最新裸 PDF 门禁、运行编号、Reader 状态和安全路线以 `docs/iteration_protocol.md` 为准；禁止在 README 复制容易失效的通过数量。
