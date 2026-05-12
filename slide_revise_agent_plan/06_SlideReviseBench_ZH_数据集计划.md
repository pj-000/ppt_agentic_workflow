# SlideReviseBench-ZH 数据集计划

## 1. 数据集目标

构建一个中文 PPT 交互式局部修改评测集：

> **SlideReviseBench-ZH**

SlideReviseBench-ZH 用于 offline computational evaluation，目标是评估系统是否能够：

```text
1. slide localization accuracy；
2. element localization accuracy；
3. operation prediction accuracy；
4. local revision quality；
5. multi-turn consistency。
```

用户研究单独评估 human-centric outcomes，包括 task completion time、number of turns、manual corrections、perceived control、trust、satisfaction、preference 和 qualitative feedback。

---

## 2. 数据集为什么重要

如果没有 benchmark，论文容易变成系统 demo。

有了 SlideReviseBench-ZH，论文就有：

```text
- 任务定义；
- gold label；
- baseline 对比；
- 消融实验；
- 可复现评价。
```

这对 IUI/CCF-B 很关键。

---

## 3. 数据来源

建议使用四类 PPT：

```text
1. 自己生成的教学类 PPT；
2. 公开课程 PPT；
3. 自己制作/改写的合成 PPT；
4. 从公开讲义生成的匿名化 PPT。
```

注意：

```text
- 避免直接开源有版权风险的完整 PPT；
- 可开源匿名化 PPT 或小规模合成 PPT；
- 内部实验可以用更多 PPT，但公开补充材料只放可授权样本；
- 数据说明中写清楚来源和授权情况。
```

---

## 4. 数据规模目标

### Pilot 版本

```text
10 套 PPT
100 条单轮修改任务
10 组多轮修改任务
```

用途：调试系统和评测脚本。

### Main 版本

```text
40–60 套 PPT
500 条单轮修改任务
50–80 组多轮修改任务
```

用途：论文主实验。

### User Study 版本

```text
6–8 套 PPT
24–36 个用户任务
```

用途：用户研究。

---

## 5. 每条单轮任务 Schema

```json
{
  "task_id": "revise_001",
  "deck_id": "deck_001",
  "initial_ppt": "decks/deck_001.pptx",
  "deck_metadata": {
    "topic": "Transformer 注意力机制",
    "domain": "人工智能",
    "audience": "本科生",
    "slide_count": 12
  },
  "interaction_context": {
    "selected_slide_id": 6,
    "selected_element_ids": [],
    "current_view": "slide_preview",
    "history": []
  },
  "user_instruction": "这页太抽象了，给学生加一个简单例子",
  "intent_type": "add_example",
  "gold_targets": {
    "affected_slides": [6],
    "affected_elements": ["s06_e02"],
    "new_slide_position": 7
  },
  "gold_operations": [
    {
      "op": "REWRITE_TEXT_BLOCK",
      "target_slide_id": 6,
      "target_element_id": "s06_e02"
    },
    {
      "op": "ADD_SLIDE",
      "after_slide_id": 6,
      "slide_type": "example",
      "content_intent": "add_example"
    }
  ],
  "constraints": {
    "preserve_unaffected_slides": true,
    "keep_original_style": true,
    "max_new_slides": 1
  },
  "notes": "当前页包含抽象定义，适合新增一页例题。"
}
```

---

## 6. 多轮任务 Schema

```json
{
  "multi_turn_id": "multi_001",
  "deck_id": "deck_001",
  "initial_ppt": "decks/deck_001.pptx",
  "turns": [
    {
      "turn_id": "t1",
      "interaction_context": {
        "selected_slide_id": 6,
        "selected_element_ids": []
      },
      "user_instruction": "这里加一个简单例子",
      "intent_type": "add_example",
      "gold_targets": {
        "affected_slides": [6]
      },
      "gold_operations": [
        {
          "op": "ADD_SLIDE",
          "after_slide_id": 6,
          "slide_type": "example",
          "content_intent": "add_example"
        }
      ]
    },
    {
      "turn_id": "t2",
      "interaction_context": {
        "selected_slide_id": null,
        "selected_element_ids": [],
        "history_reference": "刚才新增的那页"
      },
      "user_instruction": "把刚才新增的那页讲得更简单一点",
      "intent_type": "simplify_language",
      "gold_targets": {
        "affected_slides": [7]
      },
      "gold_operations": [
        {
          "op": "REWRITE_TEXT_BLOCK",
          "target_slide_id": 7,
          "content_intent": "simplify_language"
        }
      ]
    }
  ]
}
```

---

## 7. 任务类型设计

至少覆盖 12 类任务：

| 类型 | 示例 | Gold intent / primitive operation |
|---|---|---|
| 加例子 | “这里加一个适合本科生的例子” | add_example → ADD_SLIDE 或 ADD_TEXT_BLOCK |
| 扩展内容 | “这页讲得太简单，展开一点” | expand_content → ADD_TEXT_BLOCK 或 REWRITE_TEXT_BLOCK |
| 压缩内容 | “这页太满，精简一下” | compress_content → REWRITE_TEXT_BLOCK |
| 拆页 | “这一页内容太密，拆成两页” | SPLIT_SLIDE |
| 合并页 | “这两页内容重复，合并一下” | MERGE_SLIDES |
| 删除内容 | “删掉高级推导部分” | DELETE_TEXT_BLOCK / DELETE_SLIDE |
| 调整顺序 | “先讲例子，再讲公式” | REORDER_SLIDE |
| 改难度 | “改成适合本科低年级” | change_audience_level / simplify_language → REWRITE_TEXT_BLOCK |
| 改风格 | “改成答辩风格” | CHANGE_STYLE |
| 加图示 | “把这段改成流程图” | add_diagram → ADD_TEXT_BLOCK 或 ADD_SLIDE |
| 全局统一 | “统一标题和配色风格” | change_style → CHANGE_STYLE |
| 多轮引用 | “把刚才新增的那页再简单一点” | simplify_language → REWRITE_TEXT_BLOCK |

每类至少 30 条，Main 版本共 500 条左右。

---

## 8. 标注内容

每条任务至少标注：

```text
1. user_instruction
2. selected_slide_id
3. selected_element_ids
4. gold_affected_slides
5. gold_affected_elements
6. gold_operations
7. preserve_unaffected_slides
8. expected_behavior
9. notes
```

对于多轮任务，还要标注：

```text
1. previous_turn_reference
2. newly_created_slide_id
3. reference_resolution_gold
4. index_update_expectation
```

---

## 9. 标注流程

### Step 1：准备 PPT

```text
- 收集/生成 PPT；
- 解析成 deck_index；
- 检查 slide_id 和 element_id 是否稳定；
- 保存截图。
```

### Step 2：设计修改请求

每套 PPT 设计 8–12 条修改任务：

```text
- 4 条页面级任务；
- 3 条元素级任务；
- 2 条结构级任务；
- 1–2 条全局/多轮任务。
```

### Step 3：标注 Gold

人工标注：

```text
- 应该修改哪些页；
- 应该修改哪些元素；
- 应该使用哪些操作；
- 哪些页面不应该被改。
```

### Step 4：质量控制

```text
- 至少 20% 样本双人标注；
- 计算标注一致性；
- 不一致样本讨论修正；
- 记录标注指南。
```

---

## 10. 标注指南摘要

### Gold affected slides

标注所有必须被修改的页面。

如果修改请求是：

```text
“这页太满，拆成两页”
```

Gold：

```text
affected_slides = [当前页]
new_slide_position = 当前页后
operations = [SPLIT_SLIDE]
```

如果请求是：

```text
“删掉高级推导部分”
```

Gold 应包含所有含高级推导的页面。

### Gold affected elements

只标注真正需要修改的元素。

如果请求是整页风格修改，可以为空或标注整页元素。

### Gold operations

允许多个结构化 primitive operation，例如：

```json
[
  {
    "op": "REWRITE_TEXT_BLOCK",
    "target_slide_id": 6,
    "target_element_id": "s06_e02"
  },
  {
    "op": "ADD_SLIDE",
    "after_slide_id": 6,
    "slide_type": "example",
    "content_intent": "add_example"
  }
]
```

### Preserve constraints

标注哪些页面不应修改，方便计算 minimal change。

---

## 11. 数据格式建议

目录结构：

```text
experiments/slide_revise/data/SlideReviseBench-ZH/
  decks/
    deck_001.pptx
    deck_002.pptx
  indexes/
    deck_001_index.json
    deck_002_index.json
  screenshots/
    deck_001/slide_001.png
  tasks/
    single_turn.jsonl
    multi_turn.jsonl
  annotations/
    annotation_guidelines.md
    annotator_a.jsonl
    annotator_b.jsonl
  splits/
    train.jsonl      # 可选，不训练模型时也可叫 dev
    dev.jsonl
    test.jsonl
```

---

## 12. 数据集统计表

论文中需要统计：

| 统计项 | 数值 |
|---|---:|
| # Decks | 40–60 |
| # Slides | 约 500–900 |
| # Single-turn tasks | 500 |
| # Multi-turn sessions | 50–80 |
| Avg slides per deck | 10–15 |
| Avg elements per slide | 待统计 |
| # Task types | 12 |
| # Annotators | 2–3 |

---

## 13. 数据集开源策略

由于当前 PPT 生成流程和部分 PPT 可能不适合开源：

建议开源：

```text
- annotation schema
- evaluation scripts
- synthetic/anonymized subset
- task JSONL without private content
- index schema
- sample PPTs made by yourself
```

不建议开源：

```text
- 内部业务 PPT
- 私有生成流程
- 私有 prompt
- 未授权公开课程 PPT
```

论文中可以写：

> We will release the benchmark schema, evaluation scripts, and an anonymized subset of SlideReviseBench-ZH for reproducibility. The framework is backend-agnostic and does not require access to our internal slide generation backend.

---

## 14. MVP 数据集底线

如果时间紧，最低版本：

```text
20 套 PPT
300 条单轮任务
30 组多轮任务
2 个标注者标注 20% 样本
```

理想版本：

```text
40–60 套 PPT
500 条单轮任务
50–80 组多轮任务
12–18 人用户研究
```
