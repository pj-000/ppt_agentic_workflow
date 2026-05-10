原 `vendor/anthropic_pptx_skill/local_rules.md` 的迁移映射：

- `## 1. Hard Constraints` -> `slide_rules.md`
- `## 2. Visual Design Principles` -> `theme_rules.md`
- `## 3. Layout Vocabulary` -> `layout_rules.md`
- `## 4. Selection Rules` -> `layout_rules.md`
- `## 5. Style Mapping Rules` -> `theme_rules.md`
- `## 6. Preferred Generation Heuristics` -> `theme_rules.md`
- `## 6. Quality Bar` -> `slide_rules.md`

该文件已从 vendor 目录移除，运行时由 `PromptComposer.compose_local_visual_rules()` 按原顺序重组。
