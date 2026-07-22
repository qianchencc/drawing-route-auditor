# 文档导航

本目录同时保存当前运行合同、决策树审查快照、研究材料和开发评估数据。它们的权威等级不同；文件位于 `docs/` 不代表可以进入路线推理。

## 权威层级

| 层级 | 文件 | 用途 | 可进入运行时推理 |
|---|---|---|---|
| 运行合同 | 根目录 `AGENTS.md`、`README.md` | PDF-only 约束、CLI、安装和验证 | 约束实现，不作为工艺事实 |
| 当前审计 | `iteration_protocol.md` | 当前门禁、运行编号、Reader 状态、安全路线和剩余边界 | 否 |
| 决策树快照 | `decision_tree.json` | 空数据库初始化、当前 PostgreSQL 树审查和灾备 | 仅通过 `tree init` 或增量迁移进入数据库 |
| 原始研究 | `base_research.md` | 长期架构、数据结构和伪代码 | 否；不保证等于当前实现 |
| 客户来源 | `drawing_process_tree_original.json` | 客户原始决策树只读档案 | 否 |
| 开发评估 | `cases/`、`routes_*.csv`、`index.csv` | 推荐完成后的历史比较和人工审查 | 禁止 |
| 主数据参考 | `operations.csv`、`rules.csv`、`teams.csv`、`work_centers.csv` | 后续工序主数据研究 | 当前禁止 |

发生冲突时，优先级依次为：仓库约束、当前代码和数据库合同、`README.md`、`iteration_protocol.md`、研究与案例材料。历史答案永远不能覆盖 PDF 事实。

## 系统数据流

```text
PDF
→ Poppler 渲染与职责视图
→ 4 个 Reader 并发提取图纸事实
→ 合并观察并检查冲突/覆盖
→ PostgreSQL 当前决策树执行事实闭包
→ 组装 complete / complete_with_candidates / partial / error
→ 持久化运行、事实、规则命中、工序和证据
→ 推荐完成后，才可加载历史 CSV 做开发评估
```

四个 Reader 的固定职责：

1. `document_structure_reader`：标题栏、材料、数量和 BOM；
2. `geometry_dimension_reader`：原始形态、全局几何、尺寸、孔槽、板厚/管壁和加工特征；
3. `symbol_relation_reader`：焊接符号、焊缝、装配关系和大型精密内圆；
4. `requirement_annotation_reader`：技术要求、公差、表面、清洁、检验和焊缝修平。

图号、名称和 PDF 文件名可以保存为审计元数据，但决策规则不得引用它们。材料栏中的“方管”“圆管”“板”等属于 PDF 可观察的制造形态，不是身份键。

## 运行入口

```bash
.venv/bin/draw-route db wait
.venv/bin/draw-route db migrate
.venv/bin/draw-route tree validate drawing-process-tree
.venv/bin/draw-route route /path/to/drawing.pdf
```

机器输出和完整性门禁：

```bash
.venv/bin/draw-route route /path/to/drawing.pdf --format json
.venv/bin/draw-route route /path/to/drawing.pdf --require-complete
```

`route` 不接受物料编码、外部事实、上级类型或案例上下文。最终转序、材料定额、设备、班组和工时当前不在 PDF-only 范围内。

## 决策树维护

业务上只维护一棵当前树。数据库内部修订用于固定并发运行和历史证据，不是供业务选择的版本。

### 首次初始化

```bash
.venv/bin/draw-route tree init docs/decision_tree.json
.venv/bin/draw-route tree validate drawing-process-tree
```

### 增量更新

每个知识变更使用新的迁移版本，并提供：

```text
src/drawing_route_auditor/db/sql/NNNN_description.sql
src/drawing_route_auditor/db/data/NNNN_description.json
```

同时在 `src/drawing_route_auditor/db/migrations.py` 注册数据文件和 Python 迁移入口，并在 `src/drawing_route_auditor/db/knowledge_migrations.py` 实现幂等检查。已应用迁移不得改写；修复只能追加更高版本。

应用和导出：

```bash
.venv/bin/draw-route db migrate
.venv/bin/draw-route tree validate drawing-process-tree
.venv/bin/draw-route tree export drawing-process-tree > docs/decision_tree.json
```

补丁和迁移中的事实必须是 PDF 可观察事实或由这些事实派生的稳定语义。禁止按物料编码、文件名、图号、名称、CSV 行号或已知答案选择规则、路线模板、工序顺序和重复次数。

## 单图审查流程

1. 仅运行裸命令：

   ```bash
   .venv/bin/draw-route route /path/to/drawing.pdf
   ```

2. 记录运行编号、状态、总耗时、四个 Reader 状态和预测工序序列。
3. 检查每道工序是否能追溯到 PDF 页码、区域和原文证据。
4. 推荐和证据全部持久化后，才加载 `golden_route.json` 或历史 CSV。
5. 历史答案有额外工序时，先寻找可泛化的 PDF 特征和负例；找不到则保持 `partial`，不能复制答案。
6. 将当前结论写入 `iteration_protocol.md`。案例目录只保存开发审查材料，不是运行时知识源。

`cases/manifest.csv` 是开发工作清单，其中的物料编码、路径、状态和运行编号禁止成为 Reader 输入或规则条件。`context.json` 与 `golden_route.json` 只能用于后置评估。

## 结果状态

| 状态 | 合同 |
|---|---|
| `complete` | 当前范围内存在唯一完整路线，且没有未解决事实 |
| `complete_with_candidates` | 基础路线完整，存在有限且完整的工序候选集合 |
| `partial` | 只输出有证据的安全局部工序，并暴露缺失事实 |
| `error` | 没有可用工序，或完整性守卫阻止形成路线 |

`complete` 不表示与历史答案完全一致；`partial` 也不是失败掩饰。两者描述的是当前 PDF 证据和决策合同是否闭合。

## 验证门禁

```bash
ruff format --check src tests
ruff check src tests
.venv/bin/python -m pytest -q
.venv/bin/draw-route tree validate drawing-process-tree
.venv/bin/draw-route db migrate
```

行为变更还必须运行受影响的裸 PDF。每个报告结果都要包含运行编号、状态、耗时、Reader 状态和精确工序序列或候选。

## 文件更新责任

- CLI 或环境变量变化：更新根 `README.md` 和 `.env.example`。
- Reader、事实、规则或迁移变化：更新 `decision_tree.json` 和迁移幂等测试。
- 门禁结论变化：更新 `iteration_protocol.md`，不要在多个文件复制易失的通过数量。
- 研究方向变化：更新 `base_research.md`，并明确哪些内容尚未实现。
- 案例审查变化：更新对应 `cases/<id>/` 和 `manifest.csv`；这些变化不得反向影响推理。
