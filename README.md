# 津南图纸到工艺路线

`drawing-route-auditor` 是一个面向制造图纸的异步工艺路线原型：输入 PDF 图纸，由多个视觉 Reader 并发提取可审计事实，再由版本化决策树生成一条确定路线、有限候选路线，或带局部问题的非完整结果。

当前实现是开发评估系统，不是可直接回写 PLM/ERP 的生产系统。路线状态不是准确率声明；只有通过案例门禁的知识版本才可继续扩展。

## 核心约束

- Reader 只读取图纸事实，不直接生成工序或路线。
- 每个事实保存状态、对象、覆盖情况和图纸证据。
- 决策规则和 Reader 合同均绑定决策树版本。
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
→ 执行版本化决策树和事实闭包
→ 组装确定路线或候选路线
→ 校验基础路线完整性
→ 持久化运行、事实、决策、工序和证据
→ 可选：后置加载历史 CSV 进行开发评估
```

当前决策树 v3 定义四类 Reader：

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

导入并启用当前决策树：

```bash
.venv/bin/draw-route tree import docs/decision_tree_v3.json
.venv/bin/draw-route tree validate --version 3
.venv/bin/draw-route tree activate --version 3
```

最后检查运行环境：

```bash
.venv/bin/draw-route doctor
```

## 决策树维护

不指定树键和版本时，`show`、`validate` 和 `evaluate` 默认使用当前启用版本：

```bash
.venv/bin/draw-route tree list
.venv/bin/draw-route tree show
.venv/bin/draw-route tree show --format json
.venv/bin/draw-route tree validate
.venv/bin/draw-route tree evaluate --facts facts.json
```

导入是按树键、版本和来源哈希管理的。启用前必须先校验；校验中的“候选”表示决策树有意保留有限分支，不等于结构错误。

## 生成路线

默认表格输出带实时阶段进度、Reader 结果、逐工序决策、事实依据和图纸证据：

```bash
.venv/bin/draw-route route /path/to/drawing.pdf
```

机器可读输出：

```bash
.venv/bin/draw-route route /path/to/drawing.pdf --format json
```

固定树版本复现历史运行：

```bash
.venv/bin/draw-route route /path/to/drawing.pdf --version 3
```

要求路线必须完整，否则返回非零退出码：

```bash
.venv/bin/draw-route route /path/to/drawing.pdf --require-complete
```

开发评估必须同时提供物料编码：

```bash
.venv/bin/draw-route route /path/to/drawing.pdf \
  --material-code DEMO-PLATE-001 \
  --evaluate
```

`--evaluate` 先持久化模型推荐，再读取 `docs/routes_1.csv` 和 `docs/routes_2.csv`。历史答案不会进入 Reader 或决策树上下文。

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
- 图纸页码、区域和原文证据；
- 规则版本。

每条候选都会打印完整工序序列；终端在全部路线之后集中打印一次候选差异和仍需事实。

## 数据与运行产物

- PostgreSQL：决策树版本、Reader 请求、事实观察、规则命中、路线候选、逐工序决策和开发评估；
- `.runtime/rendered/`：按 PDF 哈希和 DPI 缓存的渲染页；
- `.runtime/probe/`：为 Reader 生成的图纸视图；
- `docs/cases/`：隔离的开发案例上下文、历史答案和人工审查记录。

`.runtime/` 是可再生缓存，不应作为知识源提交。

## 验证

```bash
ruff check src tests
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest
```

集成测试使用 `DRA_DATABASE_URL` 指向的 PostgreSQL。测试会迁移数据库，但不会调用真实视觉模型。

## 文档索引

- `docs/base_research.md`：原始研究与长期目标，不代表当前已实现行为；
- `docs/iteration_protocol.md`：当前单图门禁、并发边界和结果语义；
- `docs/decision_tree_v3.json`：当前 Reader、事实和规则的可导入定义；
- `docs/cases/`：按物料保存的开发案例证据和审查结论。

## 已知边界

- CLI 当前只接收 PDF 和可选物料编码，尚未接入正式 PLM/BOM 上下文。
- v3 对板类和非板精加工零件只覆盖基础路线族；复杂部件的完整路线模板仍不充分。
- 父级制造类型属于外部事实，缺失时转序可能保留候选。
- 当前不生成可直接下发的材料定额、设备、班组和工时。
- `DEMO-PLATE-001` 斗体案例仍未通过准确性门禁；详见对应案例审查。
