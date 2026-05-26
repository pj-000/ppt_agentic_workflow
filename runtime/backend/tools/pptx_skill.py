"""
backend/tools/pptx_skill.py

本地封装 Anthropic 官方 PPTX skill（vendor/anthropic_pptx_skill）。

能力：
1. run_js(code, output_path) — 执行 PptxGenJS 代码生成 .pptx
2. read_pptx(path) — 用 vendored SKILL 推荐的 markitdown 提取文本
3. pptx_to_images(path) — 调用 vendored scripts/office/soffice.py + pdftoppm 转图
4. skill_paths() — 返回本地 skill 路径，便于 planner 读取本地文档
"""

import logging
import os
import re
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = PROJECT_ROOT / "vendor" / "anthropic_pptx_skill"
SCRIPTS_ROOT = SKILL_ROOT / "scripts"
OFFICE_ROOT = SCRIPTS_ROOT / "office"

# 全局 node_modules 路径
_NPM_PREFIX = subprocess.run(["npm", "config", "get", "prefix"], capture_output=True, text=True, timeout=10).stdout.strip()
NODE_PATH = os.path.join(_NPM_PREFIX, "lib", "node_modules")

PRESENTATIONML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWINGML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

for _prefix, _namespace in {
    "p": PRESENTATIONML_NS,
    "a": DRAWINGML_NS,
    "r": OFFICE_REL_NS,
    "": CONTENT_TYPES_NS,
}.items():
    ET.register_namespace(_prefix, _namespace)

PRESENTATION_CHILD_ORDER = (
    "sldMasterIdLst",
    "notesMasterIdLst",
    "handoutMasterIdLst",
    "sldIdLst",
    "sldSz",
    "notesSz",
    "smartTags",
    "embeddedFontLst",
    "custShowLst",
    "photoAlbum",
    "custDataLst",
    "kinsoku",
    "defaultTextStyle",
    "modifyVerifier",
    "extLst",
)
PRESENTATION_CHILD_RANK = {name: index for index, name in enumerate(PRESENTATION_CHILD_ORDER)}


def skill_paths() -> dict:
    """返回本地 vendored skill 的关键路径。"""
    return {
        "root": str(SKILL_ROOT),
        "skill_md": str(SKILL_ROOT / "SKILL.md"),
        "pptxgenjs_md": str(SKILL_ROOT / "pptxgenjs.md"),
        "thumbnail_py": str(SCRIPTS_ROOT / "thumbnail.py"),
        "soffice_py": str(OFFICE_ROOT / "soffice.py"),
        "unpack_py": str(OFFICE_ROOT / "unpack.py"),
        "pack_py": str(OFFICE_ROOT / "pack.py"),
        "validate_py": str(OFFICE_ROOT / "validate.py"),
    }


def assert_skill_present():
    """确保本地 vendored skill 存在。"""
    required = [
        SKILL_ROOT / "SKILL.md",
        SKILL_ROOT / "pptxgenjs.md",
        SCRIPTS_ROOT / "thumbnail.py",
        OFFICE_ROOT / "soffice.py",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError("本地 Anthropic PPTX skill 不完整，缺少: " + ", ".join(missing))


def _xml_local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _repair_presentation_xml_child_order(xml_bytes: bytes) -> tuple[bytes, bool]:
    root = ET.fromstring(xml_bytes)
    children = list(root)
    if not children:
        return xml_bytes, False

    def sort_key(item: tuple[int, ET.Element]) -> tuple[int, int]:
        original_index, element = item
        local_name = _xml_local_name(element.tag)
        return PRESENTATION_CHILD_RANK.get(local_name, len(PRESENTATION_CHILD_ORDER) + original_index), original_index

    reordered = [element for _, element in sorted(enumerate(children), key=sort_key)]
    if reordered == children:
        return xml_bytes, False

    root[:] = reordered
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), True


def _repair_content_types_missing_overrides(xml_bytes: bytes, package_names: set[str]) -> tuple[bytes, int]:
    root = ET.fromstring(xml_bytes)
    removed_count = 0
    override_tag = f"{{{CONTENT_TYPES_NS}}}Override"

    for override in list(root.findall(override_tag)):
        part_name = str(override.attrib.get("PartName") or "").lstrip("/")
        if part_name and part_name not in package_names:
            root.remove(override)
            removed_count += 1

    if removed_count == 0:
        return xml_bytes, 0
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), removed_count


def _toggle_bool_xml_attr(element: ET.Element, attr: str) -> None:
    current = str(element.attrib.get(attr, "")).strip().lower() in {"1", "true", "on"}
    if current:
        element.attrib.pop(attr, None)
    else:
        element.set(attr, "1")


def _repair_line_shape_negative_extents(xml_bytes: bytes) -> tuple[bytes, int]:
    root = ET.fromstring(xml_bytes)
    repaired_count = 0
    sp_tag = f"{{{PRESENTATIONML_NS}}}sp"
    xfrm_tag = f"{{{DRAWINGML_NS}}}xfrm"
    off_tag = f"{{{DRAWINGML_NS}}}off"
    ext_tag = f"{{{DRAWINGML_NS}}}ext"
    geom_tag = f"{{{DRAWINGML_NS}}}prstGeom"

    for shape in root.findall(f".//{sp_tag}"):
        geometry = shape.find(f".//{geom_tag}")
        if geometry is None or geometry.attrib.get("prst") != "line":
            continue

        transform = shape.find(f".//{xfrm_tag}")
        if transform is None:
            continue
        offset = transform.find(off_tag)
        extent = transform.find(ext_tag)
        if offset is None or extent is None:
            continue

        try:
            x = int(offset.attrib.get("x", "0"))
            y = int(offset.attrib.get("y", "0"))
            cx = int(extent.attrib.get("cx", "0"))
            cy = int(extent.attrib.get("cy", "0"))
        except ValueError:
            continue

        if cx < 0:
            offset.set("x", str(x + cx))
            extent.set("cx", str(-cx))
            _toggle_bool_xml_attr(transform, "flipH")
            repaired_count += 1
        if cy < 0:
            offset.set("y", str(y + cy))
            extent.set("cy", str(-cy))
            _toggle_bool_xml_attr(transform, "flipV")
            repaired_count += 1

    if repaired_count == 0:
        return xml_bytes, 0
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), repaired_count


def _repair_slide_layout_missing_type(xml_bytes: bytes) -> tuple[bytes, bool]:
    root = ET.fromstring(xml_bytes)
    if _xml_local_name(root.tag) != "sldLayout" or root.attrib.get("type"):
        return xml_bytes, False

    root.set("type", "blank")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), True


def _notes_slides_are_empty(payload_by_name: dict[str, bytes]) -> bool:
    text_tag = f"{{{DRAWINGML_NS}}}t"
    notes_slide_names = [
        name
        for name in payload_by_name
        if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml")
    ]
    if not notes_slide_names:
        return False

    for name in notes_slide_names:
        try:
            root = ET.fromstring(payload_by_name[name])
        except ET.ParseError:
            return False

        for text in root.findall(f".//{text_tag}"):
            value = (text.text or "").strip()
            if value and not value.isdigit():
                return False

    return True


def _remove_notes_relationships(xml_bytes: bytes) -> tuple[bytes, int]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return xml_bytes, 0

    removed_count = 0
    for relationship in list(root):
        relationship_type = relationship.attrib.get("Type", "")
        if relationship_type.endswith("/notesSlide") or relationship_type.endswith("/notesMaster"):
            root.remove(relationship)
            removed_count += 1

    if removed_count == 0:
        return xml_bytes, 0
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), removed_count


def _remove_notes_master_id_list(xml_bytes: bytes) -> tuple[bytes, bool]:
    root = ET.fromstring(xml_bytes)
    removed = False
    notes_master_tag = f"{{{PRESENTATIONML_NS}}}notesMasterIdLst"

    for element in list(root):
        if element.tag == notes_master_tag:
            root.remove(element)
            removed = True

    if not removed:
        return xml_bytes, False
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), True


def _remove_content_type_overrides_for_parts(xml_bytes: bytes, removed_parts: set[str]) -> tuple[bytes, int]:
    root = ET.fromstring(xml_bytes)
    removed_count = 0
    override_tag = f"{{{CONTENT_TYPES_NS}}}Override"

    for override in list(root.findall(override_tag)):
        part_name = str(override.attrib.get("PartName") or "").lstrip("/")
        content_type = str(override.attrib.get("ContentType") or "")
        if (
            part_name in removed_parts
            or part_name.startswith("ppt/notesSlides/")
            or part_name.startswith("ppt/notesMasters/")
            or content_type.endswith("notesSlide+xml")
            or content_type.endswith("notesMaster+xml")
        ):
            root.remove(override)
            removed_count += 1

    if removed_count == 0:
        return xml_bytes, 0
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), removed_count


def _remove_empty_notes_parts(payload_by_name: dict[str, bytes]) -> int:
    if not _notes_slides_are_empty(payload_by_name):
        return 0

    removed_parts = {
        name
        for name in payload_by_name
        if name.startswith("ppt/notesSlides/") or name.startswith("ppt/notesMasters/")
    }
    if not removed_parts:
        return 0

    repaired_count = len(removed_parts)
    for name in removed_parts:
        payload_by_name.pop(name, None)

    presentation_name = "ppt/presentation.xml"
    if presentation_name in payload_by_name:
        repaired_xml, did_repair = _remove_notes_master_id_list(payload_by_name[presentation_name])
        if did_repair:
            payload_by_name[presentation_name] = repaired_xml
            repaired_count += 1

    content_types_name = "[Content_Types].xml"
    if content_types_name in payload_by_name:
        repaired_xml, removed_overrides = _remove_content_type_overrides_for_parts(
            payload_by_name[content_types_name],
            removed_parts,
        )
        if removed_overrides:
            payload_by_name[content_types_name] = repaired_xml
            repaired_count += removed_overrides

    for name in list(payload_by_name.keys()):
        if not name.endswith(".rels"):
            continue
        repaired_xml, removed_relationships = _remove_notes_relationships(payload_by_name[name])
        if removed_relationships:
            payload_by_name[name] = repaired_xml
            repaired_count += removed_relationships

    return repaired_count


def repair_pptx_office_compatibility(pptx_path: str) -> int:
    """
    修复 PptxGenJS 偶发生成的 Office 包结构兼容性问题。

    已覆盖的线上样例问题：
    1. ppt/presentation.xml 子节点顺序不符合 OOXML schema。
    2. [Content_Types].xml 声明了 zip 包内并不存在的 Override Part。
    3. line shape 使用负数 cx/cy，改为正尺寸 + flipH/flipV 保持视觉方向。
    4. slideLayout 缺少 type 属性，补为 blank。
    5. 空备注页附带的 notesMaster/notesSlide 结构会触发 Office 修复，导出前移除。
       真实演讲稿不移除，电子书生成 PPT 后续会再写入干净的 notes 结构。
    """
    target = Path(pptx_path)
    if not target.exists() or target.suffix.lower() != ".pptx":
        return 0

    try:
        with zipfile.ZipFile(target, "r") as source_zip:
            infos = source_zip.infolist()
            payload_by_name = {info.filename: source_zip.read(info.filename) for info in infos}
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"PPTX 文件不是有效的 zip 包: {target}") from exc

    presentation_name = "ppt/presentation.xml"
    if presentation_name not in payload_by_name:
        raise RuntimeError(f"PPTX 文件缺少 {presentation_name}: {target}")

    repaired_count = 0
    repaired_xml, did_repair = _repair_presentation_xml_child_order(payload_by_name[presentation_name])
    if did_repair:
        payload_by_name[presentation_name] = repaired_xml
        repaired_count += 1

    content_types_name = "[Content_Types].xml"
    if content_types_name not in payload_by_name:
        raise RuntimeError(f"PPTX 文件缺少 {content_types_name}: {target}")
    repaired_xml, removed_overrides = _repair_content_types_missing_overrides(
        payload_by_name[content_types_name],
        set(payload_by_name.keys()),
    )
    if removed_overrides:
        payload_by_name[content_types_name] = repaired_xml
        repaired_count += removed_overrides

    removed_notes = _remove_empty_notes_parts(payload_by_name)
    if removed_notes:
        repaired_count += removed_notes

    for name in list(payload_by_name.keys()):
        if not (name.startswith("ppt/slides/slide") and name.endswith(".xml")):
            continue
        repaired_xml, repaired_lines = _repair_line_shape_negative_extents(payload_by_name[name])
        if repaired_lines:
            payload_by_name[name] = repaired_xml
            repaired_count += repaired_lines

    for name in list(payload_by_name.keys()):
        if not (name.startswith("ppt/slideLayouts/slideLayout") and name.endswith(".xml")):
            continue
        repaired_xml, did_repair = _repair_slide_layout_missing_type(payload_by_name[name])
        if did_repair:
            payload_by_name[name] = repaired_xml
            repaired_count += 1

    if repaired_count == 0:
        return 0

    tmp_file = tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".pptx",
        prefix=f"{target.stem}.compat-",
        dir=str(target.parent),
        delete=False,
    )
    tmp_path = Path(tmp_file.name)
    tmp_file.close()

    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as fixed_zip:
            for info in infos:
                if info.filename.endswith("/") or info.filename not in payload_by_name:
                    continue
                fixed_zip.writestr(info.filename, payload_by_name[info.filename])
        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    logger.info("[PptxSkill] 已修复 PPTX Office 兼容性问题: %s", target)
    return repaired_count


def _repair_common_js_syntax_errors(code: str, stderr: str) -> str:
    """
    修复 LLM 生成代码里常见的字符串闭合错误。

    优先根据 Node 报错里标出的 caret 位置，修复整块“逐行字符串化”的伪代码；
    如果不是这种情况，再定点转义出错位置左侧最近的裸引号；
    如果拿不到位置信息，再回退到少量窄范围正则修复。
    """
    if "SyntaxError" not in stderr:
        return code

    repaired = _repair_quoted_js_block_near_syntax_error(code, stderr)
    if repaired != code:
        return repaired

    repaired = _repair_quote_near_syntax_error(code, stderr)
    if repaired != code:
        return repaired

    repaired = code
    patterns = [
        # 例如：text: "内容\", options: ...
        (r'\\(["\'])\s*,\s*([A-Za-z_$][\w$]*\s*:)', r"\1, \2"),
        # 例如：const x = "内容\");
        (r'\\(["\'])\s*([}\]\),;])', r"\1\2"),
    ]

    for pattern, replacement in patterns:
        repaired = re.sub(pattern, replacement, repaired)

    return repaired


def _repair_quoted_js_block_near_syntax_error(code: str, stderr: str) -> str:
    """
    修复这类被错误生成为“逐行字符串列表”的代码块：

    {
      "let slide = pres.addSlide();",
      "slide.addShape(\u0022rect", {",
      \u0022  x: 0, y: 0, w: 1, h: 1,",
      "});"
    }

    参考现有 caret-based 修复思路：只围绕报错位置附近做定点修复，
    避免误改正常代码。
    """
    location = _extract_syntax_error_location(stderr)
    if not location:
        return code

    line_no, _caret_col = location
    lines = code.splitlines(keepends=True)
    if line_no < 1 or line_no > len(lines):
        return code

    error_index = line_no - 1
    start = None
    for idx in range(error_index, -1, -1):
        if lines[idx].strip() == "{":
            start = idx
            break
    if start is None:
        return code

    end = None
    for idx in range(error_index, len(lines)):
        if lines[idx].strip() == "}":
            end = idx
            break
    if end is None or end <= start + 1:
        return code

    decoded_lines: list[str] = []
    candidate_count = 0
    js_like_count = 0

    for idx in range(start + 1, end):
        decoded = _decode_quoted_js_line(lines[idx])
        if decoded is None:
            return code
        decoded_lines.append(decoded)
        if decoded.strip():
            candidate_count += 1
            if any(
                marker in decoded
                for marker in (
                    "slide.",
                    "pres.",
                    "let ",
                    "const ",
                    "function ",
                    "=>",
                    "addText(",
                    "addShape(",
                    "addImage(",
                    "writeFile(",
                    "background =",
                    "//",
                )
            ):
                js_like_count += 1

    if candidate_count < 3 or js_like_count < 2:
        return code

    rebuilt = lines[:start] + ["{\n"] + [line + "\n" for line in decoded_lines] + ["}\n"] + lines[end + 1 :]
    return "".join(rebuilt)


def _decode_quoted_js_line(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return ""

    if stripped.startswith('"') and (stripped.endswith('",') or stripped.endswith('"')):
        end = -2 if stripped.endswith('",') else -1
        decoded = stripped[1:end]
    elif stripped.startswith("\\u0022") and (stripped.endswith('",') or stripped.endswith('"')):
        end = -2 if stripped.endswith('",') else -1
        decoded = stripped[len("\\u0022") : end]
    else:
        return None

    decoded = decoded.replace('\\"', '"')
    decoded = decoded.replace("\\u0022", '"')
    decoded = decoded.replace("\\u0027", "'")
    return decoded


def _repair_quote_near_syntax_error(code: str, stderr: str) -> str:
    """根据 Node SyntaxError 的 caret 位置，修复该位置左侧最近的裸引号。"""
    location = _extract_syntax_error_location(stderr)
    if not location:
        return code

    line_no, caret_col = location
    lines = code.splitlines(keepends=True)
    if line_no < 1 or line_no > len(lines):
        return code

    line = lines[line_no - 1]
    line_body = line.rstrip("\r\n")
    if not line_body:
        return code

    search_start = min(max(caret_col - 1, 0), len(line_body) - 1)
    for i in range(search_start, -1, -1):
        ch = line_body[i]
        if ch not in ('"', "'"):
            continue
        if i > 0 and line_body[i - 1] == "\\":
            continue

        replacement = "\\u0022" if ch == '"' else "\\u0027"
        suffix = line[len(line_body) :]
        lines[line_no - 1] = line_body[:i] + replacement + line_body[i + 1 :] + suffix
        return "".join(lines)

    return code


def _extract_syntax_error_location(stderr: str) -> tuple[int, int] | None:
    """
    从 Node SyntaxError 输出中提取 (line_no, caret_col)。
    caret_col 为 0-based 列号。
    """
    match = re.search(r":(\d+)\n[^\n]*\n([ \t]*)\^", stderr)
    if not match:
        return None
    return int(match.group(1)), len(match.group(2))


def _cleanup_preview_images(output_dir: str) -> None:
    """清理旧的 slide 预览图和临时 PDF，避免多轮 QA 混入历史文件。"""
    if not os.path.isdir(output_dir):
        return

    for name in os.listdir(output_dir):
        lowered = name.lower()
        if (name.startswith("slide") and lowered.endswith((".jpg", ".jpeg", ".png"))) or lowered.endswith(".pdf"):
            try:
                os.unlink(os.path.join(output_dir, name))
            except FileNotFoundError:
                pass


def _slide_image_sort_key(path: str) -> tuple[int, str]:
    """按页码数字排序 slide-1.jpg / slide-01.jpg / slide-10.jpg。"""
    name = os.path.basename(path)
    match = re.search(r"(\d+)(?=\.[^.]+$)", name)
    if not match:
        return (10**9, name)
    return (int(match.group(1)), name)


def _collect_slide_images(output_dir: str) -> list[str]:
    """收集并按页码排序当前目录里的 slide 预览图。"""
    images = [os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.startswith("slide") and f.lower().endswith((".jpg", ".jpeg", ".png"))]
    return sorted(images, key=_slide_image_sort_key)


def _default_preview_dir(pptx_path: str) -> str:
    base = os.path.splitext(os.path.basename(pptx_path))[0]
    return os.path.join(os.path.dirname(pptx_path), "slides_preview", base)


def run_js(code: str, output_path: str, timeout: int = 60) -> str:
    """
    执行一段 PptxGenJS JavaScript 代码，生成 .pptx 文件。
    使用本地 node + pptxgenjs。
    """
    assert_skill_present()
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp_js = f.name

    try:
        env = os.environ.copy()
        env["NODE_PATH"] = NODE_PATH

        code_to_run = code
        last_result = None

        max_attempts = 5
        for attempt in range(max_attempts):
            if attempt > 0:
                Path(tmp_js).write_text(code_to_run, encoding="utf-8")

            result = subprocess.run(
                ["node", tmp_js],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            last_result = result

            if result.returncode == 0:
                break

            repaired = _repair_common_js_syntax_errors(code_to_run, result.stderr)
            if repaired == code_to_run:
                break

            logger.warning(
                "[PptxSkill] 检测到可修复的 JS SyntaxError，自动修复后重试（第 %s/%s 次）",
                attempt + 2,
                max_attempts,
            )
            code_to_run = repaired

        result = last_result

        if result.returncode != 0:
            raise RuntimeError(f"PptxGenJS 代码执行失败 (exit {result.returncode}):\nstderr: {result.stderr[:800]}\nstdout: {result.stdout[:200]}")

        if not os.path.isfile(output_path):
            raise RuntimeError(f"代码执行成功但未生成文件: {output_path}\nstdout: {result.stdout[:300]}")

        repair_pptx_office_compatibility(output_path)

        size = os.path.getsize(output_path)
        print(f"[PptxSkill] 生成成功: {output_path} ({size:,} bytes)")
        return output_path

    finally:
        os.unlink(tmp_js)


def check_js_syntax(code: str, timeout: int = 20) -> tuple[bool, str]:
    """
    用 `node --check` 检查 JS 语法。
    返回 (is_valid, stderr_excerpt)。
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp_js = f.name

    try:
        env = os.environ.copy()
        env["NODE_PATH"] = NODE_PATH
        result = subprocess.run(
            ["node", "--check", tmp_js],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or result.stdout or "").strip()[:1200]
    finally:
        os.unlink(tmp_js)


def read_pptx(path: str) -> str:
    """用 markitdown 提取 .pptx 文本。"""
    try:
        result = subprocess.run(
            ["python", "-m", "markitdown", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception as e:
        logger.warning(f"[PptxSkill] markitdown 失败: {e}")
        return ""


def _find_binary(name: str) -> str | None:
    """在常见路径中查找可执行文件，解决 conda 环境 PATH 不含 /opt/homebrew/bin 的问题。"""
    import shutil

    found = shutil.which(name)
    if found:
        return found
    for d in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _find_libreoffice_app_soffice() -> str | None:
    """在 macOS 上优先定位 LibreOffice.app 内部真实的 soffice 可执行文件。"""
    candidates = [
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/Applications/OpenOffice.app/Contents/MacOS/soffice",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def get_preview_runtime_diagnostics() -> dict[str, str | bool]:
    """返回缩略图预览依赖的运行时探测结果，便于上层生成用户可读提示。"""
    app_soffice = _find_libreoffice_app_soffice() if sys.platform == "darwin" else None
    soffice_bin = app_soffice or _find_binary("soffice")
    pdftoppm_bin = _find_binary("pdftoppm")
    return {
        "platform": sys.platform,
        "soffice_found": bool(soffice_bin),
        "soffice_path": soffice_bin or "",
        "pdftoppm_found": bool(pdftoppm_bin),
        "pdftoppm_path": pdftoppm_bin or "",
    }


def _build_soffice_convert_commands(pptx_path: str, output_dir: str) -> list[list[str]]:
    """构造一组可依次尝试的 soffice 转 PDF 命令。"""
    profile_dir = tempfile.mkdtemp(prefix="directionai_ppt_lo_profile_")
    convert_args = [
        f"-env:UserInstallation=file://{profile_dir}",
        "--headless",
        "--nologo",
        "--nodefault",
        "--nofirststartwizard",
        "--nolockcheck",
        "--convert-to",
        "pdf:impress_pdf_Export",
        "--outdir",
        output_dir,
        pptx_path,
    ]

    commands: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def add(command: list[str]) -> None:
        key = tuple(command)
        if key not in seen:
            seen.add(key)
            commands.append(command)

    soffice_bin = _find_binary("soffice")

    if sys.platform == "darwin":
        app_soffice = _find_libreoffice_app_soffice()
        if app_soffice:
            add(["open", "-g", "-W", "-n", "-a", "LibreOffice", "--args", *convert_args])
            add([app_soffice, *convert_args])

    if soffice_bin:
        add([soffice_bin, *convert_args])

    return commands


def pptx_to_images(pptx_path: str, output_dir: str = None) -> list[str]:
    """
    用 soffice 将 .pptx 转 PDF，再用 pdftoppm 转图片。
    macOS 上优先通过 `open -a LibreOffice --args ...` 启动应用，
    避免直接从后台 CLI 拉起 `soffice` 时在 AppKit 初始化阶段崩溃。
    """
    pdftoppm_bin = _find_binary("pdftoppm")
    preview_dir = output_dir or _default_preview_dir(pptx_path)
    soffice_commands = _build_soffice_convert_commands(pptx_path, preview_dir)
    if not soffice_commands:
        logger.warning("[PptxSkill] 未找到 soffice，跳过图片转换")
        return []
    if not pdftoppm_bin:
        logger.warning("[PptxSkill] 未找到 pdftoppm，跳过图片转换")
        return []

    if output_dir is None:
        output_dir = preview_dir
    os.makedirs(output_dir, exist_ok=True)
    _cleanup_preview_images(output_dir)

    pdf_path = os.path.join(output_dir, "temp.pdf")

    try:
        env = os.environ.copy()
        if sys.platform != "darwin":
            env["SAL_USE_VCLPLUGIN"] = "svp"

        base = os.path.splitext(os.path.basename(pptx_path))[0]
        generated_pdf = os.path.join(output_dir, f"{base}.pdf")
        last_failure = ""

        for command in soffice_commands:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )

            if result.returncode == 0 and os.path.isfile(generated_pdf):
                if os.path.isfile(pdf_path):
                    os.unlink(pdf_path)
                os.rename(generated_pdf, pdf_path)
                break

            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            last_failure = stderr or stdout or f"exit {result.returncode}"
            logger.warning(
                "[PptxSkill] soffice 转 PDF 失败（命令: %s, exit %s）: %s",
                os.path.basename(command[0]),
                result.returncode,
                last_failure[:300],
            )
        else:
            logger.warning(f"[PptxSkill] soffice 转 PDF 最终失败: {last_failure[:300]}")
            return []
    except Exception as e:
        logger.warning(f"[PptxSkill] PPTX→PDF 失败: {e}")
        return []

    if not os.path.isfile(pdf_path):
        logger.warning("[PptxSkill] PDF 文件未生成")
        return []

    try:
        prefix = os.path.join(output_dir, "slide")
        result = subprocess.run(
            [pdftoppm_bin, "-jpeg", "-r", "150", pdf_path, prefix],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(f"[PptxSkill] pdftoppm 转图片失败 (exit {result.returncode})")
            return []
    except Exception as e:
        logger.warning(f"[PptxSkill] PDF→图片 失败: {e}")
        return []
    finally:
        if os.path.isfile(pdf_path):
            os.unlink(pdf_path)

    images = _collect_slide_images(output_dir)
    print(f"[PptxSkill] 生成 {len(images)} 张预览图")
    return images
