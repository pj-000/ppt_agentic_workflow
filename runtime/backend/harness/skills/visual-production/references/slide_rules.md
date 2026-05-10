## 1. Hard Constraints（硬规则，必须遵守）

### 颜色写法
- ❌ `color: "#FF0000"`
- ✅ `color: "FF0000"`
- 原因：`#` 号会导致生成的 PPTX 文件损坏或样式异常

### 透明度写法
- ❌ 在 hex 里编码透明度：`"00000020"`
- ✅ 使用独立属性：
  ```javascript
  shadow: { type: "outer", color: "000000", opacity: 0.12 }
  ```
- 原因：8 位 hex 颜色会导致文件损坏

### 项目符号写法
- ❌ `"• 第一条"`
- ✅
  ```javascript
  slide.addText([
    { text: "第一条", options: { bullet: true, breakLine: true } },
    { text: "第二条", options: { bullet: true } }
  ], {...})
  ```
- 原因：unicode bullet 会造成双项目符号，观感差且不规范

### shadow 对象复用
- ❌ 复用同一个 shadow 对象
  ```javascript
  const shadow = { type: "outer", blur: 6, offset: 2, color: "000000", opacity: 0.15 };
  slide.addShape(..., { shadow });
  slide.addShape(..., { shadow });
  ```
- ✅ 每次用工厂函数生成新对象
  ```javascript
  const makeShadow = () => ({ type: "outer", blur: 6, offset: 2, color: "000000", opacity: 0.15 });
  slide.addShape(..., { shadow: makeShadow() });
  slide.addShape(..., { shadow: makeShadow() });
  ```
- 原因：PptxGenJS 会原地修改对象，复用会导致第二次调用异常

### 形状选择
- ❌ `ROUNDED_RECTANGLE + 矩形叠加装饰边`
- ✅ 统一使用 `RECTANGLE`
- 原因：圆角矩形无法和矩形边缘装饰条严密贴合，会露出圆角，视觉不干净

### 正文对齐
- ❌ 正文居中
- ✅ 正文必须左对齐，只有封面主标题 / closing 标题 / stat-callout 数字允许居中

### 版面利用率
- ❌ 用一个超大白框、深色块或图片容器只装很少信息，制造“看起来像设计过”的假密度
- ✅ 每个大容器都必须承担明确内容职责；如果一个区域占据主视觉面积，就必须提供足够的信息量、结构关系或解释价值

### 图文比例
- ❌ 在概念页、算法页、方法页中让 stock photo 或人物图压过正文区
- ✅ 这类页面优先用图示、流程、卡片、公式框来承载解释；图片只能作为辅助，不应主导页面

### 结构对齐
- ❌ 流程节点、卡片、箭头各自漂浮，没有共享对齐线
- ✅ 同级节点必须共享基线、列宽或间距；箭头必须连接明确锚点，不能像装饰线

### 标题装饰线
- ❌ 标题下方紧跟装饰横线
- ✅ 用空白、背景分区、边框色块、侧边色带替代
- 原因：这是 AI 幻灯片最强烈的反模式之一

## 6. Quality Bar（质量门槛）

生成的 PPT 应满足：
- 打开后看起来像设计过，而不是默认模板改字
- 至少有明显的层次关系：背景 / 主区 / 次区 / 装饰
- 不出现“标题 + 横线 + 项目符号列表”这种典型 AI 模式
- 每页的布局都能一眼看出用途不同
- 配色和主题之间要有明显关联，而不是通用蓝色套壳
- 不出现“空白很大但信息很少”的假高级感
- 不出现“图片很大但讲解很弱”的图文失衡
- 不出现流程图节点未对齐、卡片宽度乱跳、视觉重心偏到一侧的结构性问题
