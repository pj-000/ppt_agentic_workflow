## 2. Visual Design Principles（软规则，给原则不给代码）

### 配色原则
- 根据演讲主题自主选择配色，不得每次都使用蓝色系
- 主色占视觉比重 60-70%，搭配 1-2 个辅色和 1 个高亮点缀色
- 封面页和结尾页必须使用深色背景（建议明度 < 40%）
- 内容页使用浅色背景，形成“深-浅-浅…-深”的三明治结构

### 色系方向参考（根据主题自主取色）
- 科技 / 区块链 / AI → 深海蓝 / 午夜紫
- 商业 / 金融 → 墨绿 / 炭灰
- 医疗 / 健康 → 青绿 / 白
- 文化 / 创意 → 赤陶 / 珊瑚
- 建筑 / 极简 → 炭灰 / 米白
- 教育 / 活力 → 珊瑚 / 金黄

### 每页必须包含的元素
- 背景色（必须有）
- 至少一个非文字视觉元素：addShape 色块、装饰条、图标圆形、流程卡片等
- 标题下方禁止紧跟装饰横线
- 正文文字左对齐，只有主标题允许居中

### 间距与密度原则
- 保持 0.5" 左右的外边距
- 内容块之间保持 0.3-0.5" 间距
- 不要把所有内容塞满整页，留出呼吸空间
- 不要让元素贴边或过于拥挤

### 字号规范
- 幻灯片标题：36-44pt，加粗
- 小节标题：20-24pt，加粗
- 正文文字：14-16pt
- 说明文字 / 脚注：10-12pt，可用浅色

### 视觉重心原则
- 每页都要有一个明显视觉重心：大数字、卡片组、圆形图标区、半屏色块等
- 不要每页都平均用力，要有主次关系
- 一页中最好只有一个 dominant visual focal point

## 5. Style Mapping Rules（风格映射规则）

当用户显式指定 `style` 时，必须优先遵守下面的映射，不要自行切换到其他风格。

### style = auto
- 根据主题自行选择最匹配的色系与视觉母题

### style = executive
- 优先色系：Midnight Executive / Charcoal Minimal
- 倾向布局：hero-cover, stat-callout, two-column, closing
- 视觉感觉：商务、稳重、深色、高对比、少量强装饰

### style = ocean
- 优先色系：Ocean Gradient / Teal Trust
- 倾向布局：hero-cover, two-column, timeline, closing
- 视觉感觉：科技感、流动感、冷色调、清爽

### style = minimal
- 优先色系：Charcoal Minimal
- 倾向布局：hero-cover, stat-callout, card-grid, closing
- 视觉感觉：极简、留白更多、文字更克制、装饰更少但更精准

### style = coral
- 优先色系：Coral Energy
- 倾向布局：hero-cover, icon-row, card-grid, closing
- 视觉感觉：活力、教育、创意、年轻化

### style = terracotta
- 优先色系：Warm Terracotta
- 倾向布局：hero-cover, card-grid, timeline, closing
- 视觉感觉：温暖、人文、叙事感

### style = teal
- 优先色系：Teal Trust
- 倾向布局：hero-cover, icon-row, two-column, closing
- 视觉感觉：清洁、现代、健康、可信赖

### style = forest
- 优先色系：Forest & Moss
- 倾向布局：hero-cover, card-grid, timeline, closing
- 视觉感觉：自然、环保、可持续

### style = berry
- 优先色系：Berry & Cream
- 倾向布局：hero-cover, card-grid, icon-row, closing
- 视觉感觉：柔和、时尚、生活方式

### style = cherry
- 优先色系：Cherry Bold
- 倾向布局：hero-cover, stat-callout, two-column, closing
- 视觉感觉：强对比、警示感、强调性强

### 额外强约束
- 当 style 不是 auto 时，至少 80% 的页面视觉风格必须与该 style 的色系和布局倾向一致
- 不允许出现“用户指定 minimal，但整体仍是 ocean/executive 风格”的偏移
- 当 style 明确指定时，封面页和结尾页必须明显体现该风格的主色系

## 6. Preferred Generation Heuristics（生成启发式）

- 先决定整份 PPT 的视觉母题，再决定单页布局
- 视觉母题例子：
  - 左侧色带 + 圆形图标
  - 顶部深色色块 + 卡片内容区
  - 大号数字 + 细说明文字
  - 卡片背景 + 轻阴影
- 同一份 PPT 中要保持母题一致，而不是每页完全换风格

