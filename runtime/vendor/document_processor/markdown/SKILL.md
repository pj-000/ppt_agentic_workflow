---
name: markdown
description: Extract text and tables from Markdown (.md) files. Use when a user uploads a Markdown document that needs to be processed.
---

# Markdown Processing for Text Extraction

## Purpose

Extract readable text and structured tables from Markdown files. This skill feeds extracted content into downstream processing (e.g., document summarization for PPT generation).

## When to Use

- User uploads a `.md` file
- Need to extract document content for summarization, analysis, or conversion to other formats
- Markdown contains code blocks, tables, or structured content that needs to be captured

## Approach

### Step 1: Direct File Read (Simplest)

Markdown files are plain text — read directly with UTF-8 encoding.

```python
with open("document.md", "r", encoding="utf-8") as f:
    raw_text = f.read()

# Strip leading/trailing whitespace
raw_text = raw_text.strip()
```

**Why direct read:**
- Markdown is plain text, no parsing library needed
- UTF-8 encoding covers most use cases
- Fast and reliable

### Step 2: Table Extraction (Optional)

Markdown tables use pipe-delimited format. Extract tables as 2D arrays:

```python
import re

def extract_markdown_tables(text: str) -> list[list[list[str]]]:
    """
    Extract Markdown tables from text.
    Markdown table format:
      | Header 1 | Header 2 |
      |----------|----------|
      | Cell 1   | Cell 2   |
    """
    tables = []
    lines = text.split("\n")
    in_table = False
    current_table: list[list[str]] = []

    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            if current_table:
                tables.append(current_table)
                current_table = []
            in_table = False
            continue

        # Skip separator rows (|---|)
        if re.match(r"^\|[\s\-:|]+\|$", line):
            continue

        # Parse cells
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells:
            current_table.append(cells)
            in_table = True

    if current_table:
        tables.append(current_table)

    return tables
```

**When to extract tables:**
- Markdown contains data tables that summarizer should handle as structured data
- Content analysis needs to preserve tabular information
- Skip if no tables present (most .md files are pure prose)

### Step 3: Code Block Preservation

Code blocks may contain important content. Detect and optionally flag:

```python
code_block_pattern = r"```[\w]*\n(.*?)```"
code_blocks = re.findall(code_block_pattern, text, re.DOTALL)

inline_code_pattern = r"`([^`]+)`"
inline_codes = re.findall(inline_code_pattern, text)
```

## Handling Common Issues

| Problem | Solution |
|---------|----------|
| Non-UTF-8 encoding | Try `encoding="utf-8-sig"` (handles BOM) or `encoding="gbk"` |
| Empty file | Return empty string with table count 0 |
| Very large files | Read line-by-line, limit total characters (e.g., 100,000) |
| Mixed line endings | Normalize with `text.replace("\r\n", "\n")` |
| YAML frontmatter | Strip content between `---` fences if present at top |

```python
# Strip YAML frontmatter
def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text
```

## Integration Notes

For PPT generation pipeline:
1. Read raw markdown → pass to document-summarizer skill
2. Markdown naturally preserves structure (headings, lists, code) — summarizer handles section detection
3. Estimate page count: `max(1, len(text) // 3000)` (rough estimate based on character count)
4. For YAML frontmatter files: strip before passing to summarizer

## Output Format

Return `(raw_text: str, tables: list[list[list[str]]], estimated_pages: int)`:
- `raw_text`: full markdown text, stripped of leading/trailing whitespace
- `tables`: list of extracted tables, each table is `list[list[str]]` (rows → cells); empty list if no tables
- `estimated_pages`: rough page estimate based on character count
