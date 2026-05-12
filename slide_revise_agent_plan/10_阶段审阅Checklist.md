# 阶段审阅 Checklist

本文件用于每完成一个开发阶段后，让 ChatGPT 审阅代码、方法和论文风险。

使用方式：

```text
我完成了 Phase X，这是相关文件和运行结果。请按 10_阶段审阅Checklist.md 的 Phase X checklist 审阅。
```

---

# Phase 0 审阅：项目骨架与 Schema

## 你需要提供

```text
- 新增目录树
- slide_revise_agent/index/schema.py
- slide_revise_agent/cli.py
- configs/default.yaml
```

## 审阅 Checklist

```text
[ ] 是否没有破坏原有 ppt_backend/runtime？
[ ] schema 是否覆盖 DeckIndex、SlideIndex、ElementIndex？
[ ] InteractionIntent、LocalizationResult、EditPlan 是否定义清楚？
[ ] 字段是否足够支持论文里的 Revision Index？
[ ] 是否有类型标注和 Pydantic 校验？
[ ] CLI 是否可运行？
[ ] 是否有最小测试？
```

## 重点风险

```text
- schema 太工程化，缺少论文方法感；
- operation 定义不清楚；
- 没有为多轮交互留字段。
```

---

# Phase 1 审阅：PPT Parser

## 你需要提供

```text
- ppt_parser.py
- 1–3 个 deck_index.json 样例
- 解析日志
- 一份解析失败或 fallback 说明
```

## 审阅 Checklist

```text
[ ] 每页是否有稳定 slide_id？
[ ] 标题、正文、元素是否提取正确？
[ ] element_id 是否稳定且唯一？
[ ] bbox 是否合理？
[ ] 解析失败是否 graceful fallback？
[ ] 是否可以重新加载为 DeckIndex？
[ ] 是否记录图片、表格、shape 等非文本元素？
```

## 重点风险

```text
- 只能提文本，无法定位元素；
- element_id 不稳定导致后续 gold label 失效；
- bbox 单位不清楚。
```

---

# Phase 2 审阅：Semantic / Element / Dependency Index

## 你需要提供

```text
- semantic_index.py
- element_index.py
- dependency_index.py
- 增强后的 deck_index.json
```

## 审阅 Checklist

```text
[ ] 每页 summary 是否有意义？
[ ] key_concepts 是否可用于语义定位？
[ ] visual_density 是否可解释？
[ ] dependency edge 是否合理？
[ ] 是否能识别目录/章节/总结页？
[ ] 是否不依赖必须训练模型？
```

## 重点风险

```text
- summary/key concepts 全靠 LLM 且不可复现；
- dependency 只是概念重合，没有实际作用；
- visual_density 规则不清楚。
```

---

# Phase 3 审阅：Intent Parser

## 你需要提供

```text
- intent_parser.py
- reference_resolver.py
- 20 条 user_instruction 解析样例
- rule-based 和 optional LLM mode 说明
```

## 审阅 Checklist

```text
[ ] 是否支持至少 12 类 intent？
[ ] “这页”“这里”“刚才新增的那页”是否能解析？
[ ] candidate_operation_templates 是否合理，且是否使用结构化 operation objects？
[ ] 不使用 API key 是否可运行？
[ ] LLM 输出是否有 schema validation？
[ ] 错误解析是否有 fallback？
```

## 重点风险

```text
- 变成纯 prompt parsing；
- 不支持多轮历史；
- intent_type 与 operation taxonomy 不一致。
```

---

# Phase 4 审阅：Localization

## 你需要提供

```text
- scoring.py
- slide_locator.py
- element_locator.py
- 10 条定位结果样例
- ablation flags 说明
```

## 审阅 Checklist

```text
[ ] 是否融合多个定位信号？
[ ] 每个信号是否可解释？
[ ] 是否输出 confidence 和 reason？
[ ] 是否支持 top-k slide 和 top-k element？
[ ] 是否支持禁用某个信号？
[ ] 是否和 benchmark gold label 对齐？
```

## 重点风险

```text
- 只是 embedding retrieval；
- 只是 LLM direct locate；
- reason 是模型编的，不对应真实分数。
```

---

# Phase 5 审阅：Edit Planner

## 你需要提供

```text
- operation_index.py
- edit_planner.py
- 20 条 edit_plan 样例
```

## 审阅 Checklist

```text
[ ] 操作空间是否完整？
[ ] edit plan 是否可执行？
[ ] 是否有 preserve_unaffected_slides？
[ ] 每个操作是否有 reason？
[ ] 是否能处理页面级、元素级、全局级修改？
[ ] 是否支持 rule-based fallback？
```

## 重点风险

```text
- edit plan 太自然语言，不可评估；
- 操作定义不稳定；
- 定位和规划断裂。
```

---

# Phase 6 审阅：Lightweight Executor

## 你需要提供

```text
- executor 代码
- before/after PPTX
- execution_log.json
- updated deck_index.json
```

## 审阅 Checklist

```text
[ ] 修改后 PPTX 是否可打开？
[ ] 是否能执行 MVP 操作？
[ ] 是否尽量保持无关页面不变？
[ ] 是否能重新解析并更新 index？
[ ] 执行失败是否有 error log？
[ ] 是否和 internal backend 解耦？
```

## 重点风险

```text
- 执行器太弱导致 demo 难看；
- 过度依赖内部后端无法复现；
- 修改无关页面。
```

---

# Phase 7 审阅：Dynamic Index Update

## 你需要提供

```text
- updater.py
- multi-turn demo log
- deck_index_v000/v001/v002.json
```

## 审阅 Checklist

```text
[ ] 新增页后 index 是否更新？
[ ] 删除页后 slide_id 是否一致？
[ ] 历史引用是否能解析？
[ ] dependency 是否更新？
[ ] 是否支持 versioned index？
[ ] 多轮错误是否有日志？
```

## 重点风险

```text
- 第一轮后 index 失效；
- slide_id 重排导致 gold label 对不上；
- “刚才那页”解析错误。
```

---

# Phase 8 审阅：Benchmark / Evaluation

## 你需要提供

```text
- dataset_schema.py
- loader.py
- metrics.py
- eval_report.md
- results.csv
```

## 审阅 Checklist

```text
[ ] 数据 schema 是否包含 gold slides/elements/operations？
[ ] 指标是否正确？
[ ] baseline 是否公平？
[ ] ablation 是否可运行？
[ ] 结果表是否能支撑论文主张？
[ ] 是否保存完整 predictions？
```

## 重点风险

```text
- 只有 case study，没有量化；
- gold label 不清楚；
- baseline 太弱；
- 数据集规模太小。
```

---

# Phase 9 审阅：UI Demo / User Study

## 你需要提供

```text
- UI 截图或视频
- 用户任务设计
- 问卷
- pilot 用户反馈
```

## 审阅 Checklist

```text
[ ] UI 是否支持完整交互闭环？
[ ] 是否展示定位结果和解释？
[ ] 用户研究任务是否真实？
[ ] baseline 是否合理？
[ ] 指标是否符合 IUI 预期？
[ ] 是否考虑伦理说明？
```

## 重点风险

```text
- UI 像工程 demo，没有研究变量；
- 用户研究任务太简单；
- 没有对照条件。
```

---

# Phase 10 审阅：论文初稿

## 你需要提供

```text
- paper draft
- figures
- tables
- experiment results
- user study results
```

## 审阅 Checklist

```text
[ ] Introduction 是否讲清楚 one-shot generation 的不足？
[ ] Contributions 是否清晰？
[ ] Method 是否突出 Revision Index？
[ ] Experiments 是否覆盖 localization/planning/revision/user study？
[ ] Discussion 是否符合 IUI 风格？
[ ] Limitations 是否诚实但不削弱贡献？
[ ] 是否有 GenAI Usage Disclosure？
[ ] 是否匿名？
```

---

## 通用审阅问题

每次审阅都可以问：

```text
1. 这个阶段的结果是否仍然对齐 IUI 2027？
2. 方法贡献是否足够，不像 prompt engineering？
3. 是否可复现？
4. 是否会因为完整 PPT 后端不开源而被质疑？
5. 下一阶段最该补什么？
```
