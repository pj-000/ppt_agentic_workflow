# Revision Index 核心方法设计

## 1. 方法总览

Revision Index 是论文的核心创新。它不是简单的 embedding 检索，也不是让 LLM 直接猜要改哪页。

它是一个多粒度 Revision Index，用于回答三个问题：

```text
1. 用户想改哪里？
2. 具体影响哪些页面和元素？
3. 应该采用什么编辑操作？
```

完整 Index 包括五层：

```text
Interaction Context Index
Slide Semantic Index
Element Index
Dependency Index
Operation Index
```

五层 Index 的技术定位如下：

| Index 层 | 解决的技术问题 | 作用 |
|---|---|---|
| Interaction Context Index | 交互指代消解与多轮上下文维护 | 理解“这页”“这里”“刚才那部分”等用户指代 |
| Slide Semantic Index | 页面级语义表示与主题定位 | 记录每页标题、摘要、主题、关键概念、教学/表达角色 |
| Element Index | 元素级可操作对象建模 | 记录文本框、图片、图表、公式、shape、位置、样式和可编辑性 |
| Dependency Index | 页面间结构与语义依赖建模 | 记录目录、章节页、总结页、概念延续页等依赖关系 |
| Operation Index | 修改意图到编辑操作的映射 | 将用户意图映射到可执行编辑操作和约束 |

---

## 2. DeckIndex 顶层结构

建议使用 Pydantic：

```python
from pydantic import BaseModel, Field
from typing import Literal, Any

class DeckIndex(BaseModel):
    deck_id: str
    source_pptx_path: str | None = None
    slides: list[SlideIndex]
    elements: list[ElementIndex]
    dependencies: list[SlideDependency]
    interaction_history: list[InteractionTurn] = Field(default_factory=list)
    operation_index: OperationIndex
    metadata: dict[str, Any] = Field(default_factory=dict)
```

---

## 3. Interaction Context Index

### 作用

用于理解用户指代：

```text
这页
这里
刚才新增的那页
前面那部分
Transformer 那几页
这个公式
```

### Schema

```python
class InteractionContext(BaseModel):
    selected_slide_id: int | None = None
    selected_element_ids: list[str] = Field(default_factory=list)
    current_view: Literal["slide_preview", "outline", "element", "global"] = "slide_preview"
    visible_slide_ids: list[int] = Field(default_factory=list)
    history_window: list[str] = Field(default_factory=list)
    user_role: str | None = None
```

```python
class InteractionTurn(BaseModel):
    turn_id: str
    user_instruction: str
    parsed_intent: str | None = None
    affected_slide_ids: list[int] = Field(default_factory=list)
    affected_element_ids: list[str] = Field(default_factory=list)
    operations: list[dict[str, Any]] = Field(default_factory=list)
    created_slide_ids: list[int] = Field(default_factory=list)
    deleted_slide_ids: list[int] = Field(default_factory=list)
    timestamp: str | None = None
```

### 示例

```json
{
  "selected_slide_id": 7,
  "selected_element_ids": [],
  "current_view": "slide_preview",
  "history_window": [
    "上一轮新增了第 8 页例题页"
  ]
}
```

---

## 4. Slide Semantic Index

### 作用

记录每页讲了什么，用于语义定位。

### Schema

```python
class SlideIndex(BaseModel):
    slide_id: int
    title: str | None = None
    summary: str = ""
    key_concepts: list[str] = Field(default_factory=list)
    section: str | None = None
    role: str | None = None
    text: str = ""
    word_count: int = 0
    visual_density: Literal["low", "medium", "high", "unknown"] = "unknown"
    element_ids: list[str] = Field(default_factory=list)
    screenshot_path: str | None = None
    embedding_id: str | None = None
    metadata: dict = Field(default_factory=dict)
```

### 示例

```json
{
  "slide_id": 7,
  "title": "Transformer 的自注意力机制",
  "summary": "介绍 self-attention 的 Q、K、V 计算和注意力权重",
  "key_concepts": ["Self-Attention", "Query", "Key", "Value"],
  "section": "Transformer 基础",
  "role": "concept_explanation",
  "visual_density": "high"
}
```

---

## 5. Element Index

### 作用

让系统不仅能定位第几页，还能定位页面里的哪个文本框、图、公式、表格或 shape。

### Schema

```python
class BoundingBox(BaseModel):
    x: float
    y: float
    w: float
    h: float

class ElementIndex(BaseModel):
    element_id: str
    slide_id: int
    element_type: Literal[
        "title", "text", "bullet_list", "image", "shape", "table",
        "chart", "formula", "diagram", "unknown"
    ]
    text: str | None = None
    bbox: BoundingBox | None = None
    font_size: float | None = None
    style: dict = Field(default_factory=dict)
    linked_concepts: list[str] = Field(default_factory=list)
    editable: bool = True
    embedding_id: str | None = None
    metadata: dict = Field(default_factory=dict)
```

### 示例

```json
{
  "element_id": "s07_e03",
  "slide_id": 7,
  "element_type": "bullet_list",
  "text": "Self-Attention 通过 Q、K、V 计算注意力权重",
  "bbox": {"x": 1.2, "y": 2.4, "w": 6.8, "h": 1.5},
  "font_size": 18,
  "linked_concepts": ["Self-Attention", "QKV"],
  "editable": true
}
```

---

## 6. Dependency Index

### 作用

PPT 不是独立页面，修改一页可能影响：

```text
目录页
章节页
总结页
后续引用页
前置概念页
```

### Schema

```python
class SlideDependency(BaseModel):
    source_slide_id: int
    target_slide_id: int
    dependency_type: Literal[
        "agenda", "summary", "section", "concept_prerequisite",
        "concept_followup", "example_of", "style_group", "unknown"
    ]
    shared_concepts: list[str] = Field(default_factory=list)
    strength: float = 0.0
    reason: str | None = None
```

### 示例

```json
{
  "source_slide_id": 7,
  "target_slide_id": 10,
  "dependency_type": "summary",
  "shared_concepts": ["Self-Attention"],
  "strength": 0.75,
  "reason": "第 10 页总结中引用了第 7 页的 self-attention 概念"
}
```

---

## 7. Operation Index

### 作用

把用户意图映射为候选编辑操作。

### Intent 与 Primitive Operation 区分

`add_example`、`simplify_language`、`split_dense_slide`、`change_audience_level`、`change_style` 等是用户修改意图，不是 primitive edit operation。Operation Index 的作用是把这些 intent 映射为可执行操作对象。例如 `add_example` 可以映射为 `ADD_SLIDE`，并在参数中标明 `slide_type="example"` 和 `content_intent="add_example"`。

### Primitive 操作集合

#### 页面级操作

```text
ADD_SLIDE
DELETE_SLIDE
REORDER_SLIDE
SPLIT_SLIDE
MERGE_SLIDES
```

#### 元素级操作

```text
REWRITE_TEXT_BLOCK
ADD_TEXT_BLOCK
DELETE_TEXT_BLOCK
MOVE_ELEMENT
RESIZE_ELEMENT
RESTYLE_ELEMENT
```

#### 全局操作

```text
CHANGE_STYLE
UPDATE_SUMMARY
```

其他内容类需求通过 intent 和参数表达，而不是新增 primitive 操作名。

### Schema

```python
class OperationRule(BaseModel):
    intent_type: str
    candidate_operation_templates: list[dict[str, Any]]
    conditions: list[str] = Field(default_factory=list)
    preferred_operation_template: dict[str, Any] | None = None
    notes: str | None = None

class OperationIndex(BaseModel):
    rules: list[OperationRule]
```

### 示例

```json
{
  "intent_type": "make_slide_less_dense",
  "candidate_operation_templates": [
    {
      "op": "SPLIT_SLIDE",
      "target_slide_id": 6,
      "content_intent": "split_dense_slide"
    },
    {
      "op": "REWRITE_TEXT_BLOCK",
      "target_slide_id": 6,
      "content_intent": "compress_content"
    }
  ],
  "conditions": [
    "visual_density == high",
    "word_count > threshold"
  ],
  "preferred_operation_template": {
    "op": "SPLIT_SLIDE",
    "target_slide_id": 6,
    "content_intent": "split_dense_slide"
  }
}
```

---

## 8. 意图解析 Schema

```python
class InteractionIntent(BaseModel):
    raw_instruction: str
    intent_type: str
    target_scope: Literal[
        "selected_slide", "selected_element", "current_section",
        "whole_deck", "semantic_region", "unknown"
    ] = "unknown"
    explicit_references: list[str] = Field(default_factory=list)
    content_requirements: list[str] = Field(default_factory=list)
    style_requirements: list[str] = Field(default_factory=list)
    candidate_operation_templates: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.0
```

常见 intent_type：

```text
add_example
expand_content
compress_content
split_slide
merge_slides
delete_content
reorder_content
change_audience_level
change_style
add_diagram
rewrite_text
simplify_language
global_unify
resolve_reference
```

---

## 9. 定位算法

### 目标

给定：

```text
q: user instruction
c: interaction context
I: revision index
h: history
```

输出：

```text
affected slides
affected elements
confidence
reason
```

### 页面分数

我们将交互式修改中的页面/元素定位形式化为一个 training-free 的多信号排序问题。

```text
TargetScore(s) =
  α · ExplicitReferenceScore(s)
+ β · SelectionContextScore(s)
+ γ · SemanticSimilarityScore(s)
+ δ · ElementRelevanceScore(s)
+ ε · DependencyScore(s)
+ ζ · InteractionHistoryScore(s)
```

### 建议默认权重

| 信号 | 权重 |
|---|---:|
| ExplicitReferenceScore | 0.25 |
| SelectionContextScore | 0.25 |
| SemanticSimilarityScore | 0.20 |
| ElementRelevanceScore | 0.12 |
| DependencyScore | 0.08 |
| InteractionHistoryScore | 0.10 |

注意：权重先用规则设定，后期可在验证集上网格搜索。

---

## 10. 定位结果 Schema

```python
class SlideTarget(BaseModel):
    slide_id: int
    score: float
    reason: str

class ElementTarget(BaseModel):
    element_id: str
    slide_id: int
    score: float
    reason: str

class LocalizationResult(BaseModel):
    affected_slides: list[SlideTarget]
    affected_elements: list[ElementTarget]
    confidence: float
    reason: str
```

示例：

```json
{
  "affected_slides": [
    {
      "slide_id": 6,
      "score": 0.91,
      "reason": "用户当前选中第 6 页，且请求中的'这页'是显式指代"
    }
  ],
  "affected_elements": [
    {
      "element_id": "s06_e02",
      "slide_id": 6,
      "score": 0.78,
      "reason": "该文本块包含被请求扩展的核心概念 Self-Attention"
    }
  ],
  "confidence": 0.86,
  "reason": "显式选择上下文和语义匹配一致"
}
```

---

## 11. 编辑计划 Schema

```python
class EditOperation(BaseModel):
    op: str
    target_slide_id: int | None = None
    target_element_id: str | None = None
    after_slide_id: int | None = None
    slide_type: str | None = None
    content_intent: str | None = None
    instruction: str
    parameters: dict = Field(default_factory=dict)
    reason: str | None = None

class EditPlan(BaseModel):
    plan_id: str
    operations: list[EditOperation]
    preserve_slides: list[int] = Field(default_factory=list)
    requires_global_update: bool = False
    explanation: str
```

示例：

```json
{
  "plan_id": "plan_001",
  "operations": [
    {
      "op": "REWRITE_TEXT_BLOCK",
      "target_slide_id": 6,
      "target_element_id": "s06_e02",
      "instruction": "将抽象定义改写为适合本科生理解的讲解",
      "reason": "用户认为当前页太抽象"
    },
    {
      "op": "ADD_SLIDE",
      "after_slide_id": 6,
      "slide_type": "example",
      "content_intent": "add_example",
      "instruction": "新增一个简单例子解释 self-attention",
      "reason": "当前页内容密度较高，新增例子宜放入单独页面"
    }
  ],
  "preserve_slides": [1, 2, 3, 4, 5, 7, 8, 9],
  "requires_global_update": true,
  "explanation": "将第 6 页作为概念解释页保留，并在其后新增例题页，同时后续需要更新目录/页码。"
}
```

---

## 12. 动态更新机制

每次执行修改后：

```text
1. 重新解析受影响页面和新增页面；
2. 更新 SlideIndex；
3. 更新 ElementIndex；
4. 更新 DependencyIndex；
5. 记录 InteractionTurn；
6. 如果页面顺序变化，更新 slide_id 映射；
7. 保存 versioned deck_index.json。
```

建议 index version：

```text
deck_index_v000.json
deck_index_v001.json
deck_index_v002.json
```

每一轮都保存：

```text
interaction.json
localization_result.json
edit_plan.json
execution_log.json
updated_index.json
```

---

## 13. 论文中如何强调方法性

不要说：

```text
我们让 LLM 判断该改哪页。
```

要说：

```text
我们构建结构化 Revision Index，并融合显式指代、选择上下文、语义相似度、元素相关性、页面依赖和交互历史进行多信号定位。
```

不要说：

```text
我们让模型生成修改计划。
```

要说：

```text
我们将用户修改意图映射到预定义编辑操作空间，并通过 Operation Index 和页面状态约束生成局部编辑计划。
```
