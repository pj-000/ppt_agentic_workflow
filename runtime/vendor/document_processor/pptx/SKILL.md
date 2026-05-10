---
name: pptx
description: Extract text and tables from PowerPoint (.pptx) files. Use when a user uploads a PPTX presentation that needs to be processed.
---

# PowerPoint Processing for Text Extraction

## Purpose

Extract readable text and structured tables from PPTX files. This skill feeds extracted content into downstream processing (e.g., document summarization for PPT generation, content analysis).

## When to Use

- User uploads a `.pptx` file
- Need to extract presentation content for summarization, analysis, or conversion
- Presentation contains text in shapes, tables, or speaker notes
- Extract content to generate a new presentation based on existing one

## Approach

### Step 1: Try python-pptx First

python-pptx is the standard library for reading PPTX content.

```python
from pptx import Presentation

prs = Presentation("presentation.pptx")
text_parts = []
tables = []
slide_count = len(prs.slides)

for slide in prs.slides:
    for shape in slide.shapes:
        # Extract text from shapes
        if shape.has_text_frame:
            for paragraph in shape.text_frame.paragraphs:
                line = paragraph.text.strip()
                if line:
                    text_parts.append(line)

        # Extract tables
        if shape.has_table:
            table_data = []
            for row in shape.table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                table_data.append(cells)
            if table_data:
                tables.append(table_data)
```

**Why python-pptx first:**
- Native PPTX support, no external dependencies beyond the library
- Iterates all shapes (text boxes, placeholders, tables, etc.)
- Can access slide metadata (slide count, layout info)

### Step 2: Fallback to markitdown (CLI Tool)

markitdown converts PPTX to plain text/markdown for easy extraction.

```bash
python -m markitdown presentation.pptx
```

```python
import subprocess

result = subprocess.run(
    ["python", "-m", "markitdown", "presentation.pptx"],
    capture_output=True, text=True, timeout=60
)
if result.returncode == 0:
    raw_text = result.stdout.strip()
```

**Use markitdown when:**
- python-pptx is not installed
- Need richer text extraction (includes speaker notes, metadata)
- Output in markdown format is preferred

### Step 3: Alternative — markitdown as Python Library

```python
# pip install "markitdown[pptx]"
import markitdown

md_converter = markitdown.MarkItDown()
result = md_converter.convert("presentation.pptx")
raw_text = result.text_content
```

### Step 4: Raw XML Access (Advanced)

PPTX files are ZIP archives containing XML. Extract raw text if needed:

```python
import zipfile

with zipfile.ZipFile("presentation.pptx", "r") as z:
    slide_files = sorted([f for f in z.namelist() if f.startswith("ppt/slides/slide") and f.endswith(".xml")])
    for slide_file in slide_files:
        content = z.read(slide_file).decode("utf-8")
        # Extract text between <a:t> tags
        import re
        texts = re.findall(r"<a:t>([^<]+)</a:t>", content)
```

**Use XML extraction when:**
- Both python-pptx and markitdown are unavailable
- Need raw slide XML for custom processing

## Handling Common Issues

| Problem | Solution |
|---------|----------|
| Empty text (encrypted PPTX) | File may be password-protected — prompt user to unlock first |
| Text in grouped shapes | python-pptx automatically flattens groups; XML fallback may be needed |
| Slide master text bleeding through | Filter out text from slide masters/layouts (use `shape.is_placeholder` check) |
| Tables not extracting | Verify `shape.has_table` is True; some tables are images, not shapes |
| Very large presentations | Process slide-by-slide, limit total text (e.g., 100,000 characters) |
| .ppt (legacy format, not .pptx) | Convert with LibreOffice: `soffice --headless --convert-to pptx file.ppt` |
| python-pptx not installed | Use markitdown fallback |

## Content Structure Preservation

PPTX content follows a slide hierarchy:

```python
# Preserve slide-by-slide structure
for slide_idx, slide in enumerate(prs.slides):
    slide_title = None
    slide_content = []

    for shape in slide.shapes:
        if shape.has_text_frame:
            text = shape.text_frame.text.strip()
            if not text:
                continue
            # First text shape with large font is likely the title
            if shape.shape_type == 1:  # TITLE placeholder
                slide_title = text
            else:
                slide_content.append(text)

    slide_text = f"Slide {slide_idx + 1}"
    if slide_title:
        slide_text += f": {slide_title}"
    if slide_content:
        slide_text += "\n" + "\n".join(slide_content)
    all_slides.append(slide_text)
```

## Speaker Notes Extraction

Speaker notes can contain additional context:

```python
if slide.has_notes_slide:
    notes_frame = slide.notes_slide.notes_text_frame
    notes_text = notes_frame.text.strip()
```

## Integration Notes

For pptagent pipeline:
1. Extract text from each slide → join with double newlines: `"\n\n".join(text_parts)`
2. Extract tables → pass alongside text to summarizer
3. Slide count serves as page count estimate: `max(1, slide_count)`
4. Consider speaker notes as supplementary content if available

## Output Format

Return `(raw_text: str, tables: list[list[list[str]]], slide_count: int)`:
- `raw_text`: all text parts joined with double newlines
- `tables`: list of tables, each table is `list[list[str]]` (rows → cells)
- `slide_count`: total number of slides (used as page count estimate)
