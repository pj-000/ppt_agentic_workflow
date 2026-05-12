# SlideReviseAgent 研究与开发计划总览

> 目标仓库：`https://github.com/pj-000/directionai-agent-backend`  
> 目标会议：ACM IUI 2027，CCF-B，人机交互与普适计算方向  
> 论文方向：**SlideReviseAgent：基于多粒度 Revision Index 的交互式 PPT 局部修改 Agent**

---

## 1. 这套文档怎么用

这套 Markdown 文件是为了让你后续把任务拆给 Codex 执行，并且每完成一个阶段后可以交给 ChatGPT 审阅。

建议流程：

```text
1. 先读 01、02，确认论文方向和目标会议。
2. 再读 03、04，确认系统架构和核心 Index 设计。
3. 按 05 的 Phase 顺序让 Codex 实现。
4. 数据集和实验按 06、07 执行。
5. 论文写作按 08 执行。
6. 给老师汇报用 09。
7. 每个阶段完成后，用 10 的 checklist 找 ChatGPT 审阅。
8. 让 Codex 开始前，把 11 作为 master prompt 交给它。
```

---

## 2. 文件说明

| 文件 | 用途 |
|---|---|
| `00_README_计划总览.md` | 当前文件，总览整套计划 |
| `01_目标会议_IUI2027_与研究定位.md` | 为什么选 IUI 2027，以及如何对齐会议要求 |
| `02_研究问题_创新点_贡献表述.md` | 研究问题、核心贡献、创新性表述 |
| `03_仓库改造与系统架构.md` | 在当前仓库上如何新增模块，不破坏原 PPT 后端 |
| `04_Revision_Index_核心方法设计.md` | 多粒度 Revision Index 设计、schema、算法 |
| `05_Codex_分阶段实现计划.md` | Codex 可执行的开发路线、验收标准、代码结构 |
| `06_SlideReviseBench_ZH_数据集计划.md` | 数据集构建、标注 schema、任务类型和质量控制 |
| `07_实验设计与消融计划.md` | 离线实验、用户研究、指标、baseline、消融 |
| `08_IUI2027_论文写作计划.md` | 论文结构、图表、写作节奏、投稿注意事项 |
| `09_给老师汇报用_精简版.md` | 直接用于和老师沟通的汇报稿和 PPT 结构 |
| `10_阶段审阅Checklist.md` | 每完成一个阶段后发给 ChatGPT 审阅的清单 |
| `11_Codex_Master_Prompt.md` | 可直接给 Codex 的总提示词 |
| `12_风险控制与最小可发表版本.md` | 风险、备选方案、MVP 和投稿底线 |

---

## 3. 最终方向一句话

**本项目不再主打“一次性 PPT 生成”，而是主打“交互式 PPT 局部修改”。核心创新是 Multi-granularity Revision Index：它将用户交互上下文、页面语义、页面元素、页面依赖和编辑操作统一索引起来，使系统能够根据自然语言、页面选择、元素选择、大纲修改等交互意图，定位受影响页面/元素，并生成最小化的局部编辑计划。**

---

## 4. 目标论文标题

推荐主标题：

> **SlideReviseAgent: Index-Guided Interactive Local Revision for Presentation Slides**

中文汇报标题：

> **SlideReviseAgent：基于多粒度 Revision Index 的交互式 PPT 局部修改 Agent**

备选标题：

1. **Revision-Aware Indexing for Interactive Slide Deck Editing**
2. **Supporting User-Controlled Slide Revision with Multi-Granularity Revision Indexes**
3. **Index-Guided Human-AI Interaction for Local Presentation Revision**

---

## 5. 当前项目与新方向的关系

当前仓库已经有：

```text
ppt_backend/   独立 CLI 和 FastAPI 服务入口
runtime/       原 PPT 生成 runtime、prompt 模板、vendor skill 和工作区
outputs/       PPT 输出目录
```

后续不建议直接重写原有 `runtime/`。建议新增一个独立研究模块：

```text
slide_revise_agent/
```

原来的 PPT 生成后端只作为可选执行器/renderer/backend adapter，不作为论文核心创新。

---

## 6. 你后续每次找 ChatGPT 审阅时怎么说

示例：

```text
我完成了 Phase 2：PPT Parser 和基础 Revision Index。
这是相关文件：...
请你按照 10_阶段审阅Checklist.md 中 Phase 2 的标准审阅：
1. schema 是否合理；
2. index 是否够论文方法；
3. 是否会影响原仓库功能；
4. 下一步该怎么改。
```

---

## 7. 最重要的注意事项

1. 不要把论文写成“调用大模型改 PPT”。
2. 不要把核心贡献放在 Anthropic PPT Skill 或现有 PPT 生成流程上。
3. 不要一开始追求复杂前端，先把 index、定位、编辑规划、评测做好。
4. 当前完整 PPT 生成流程不适合开源没关系，论文核心应可通过 lightweight executor 复现。
5. IUI 方向必须有人本证据，至少做一个小规模用户研究。

前端界面不是论文的核心方法贡献，但为了 IUI 投稿、demo 展示和用户研究，需要实现一个轻量级 research prototype。界面支持上传 PPT、浏览页面缩略图、选择页面/元素、输入自然语言修改请求、展示系统定位结果、编辑计划和修改前后对比。论文主贡献仍然是 Multi-granularity Revision Index 以及 index-guided localization/planning。
