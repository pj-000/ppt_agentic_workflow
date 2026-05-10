from __future__ import annotations

import functools
import fnmatch
import re
from pathlib import Path

from backend.harness.contracts.skills import (
    SkillAssetKind,
    SkillAssetSpec,
    SkillPhase,
    SkillReference,
    SkillSpec,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT_ROOT / "harness" / "skills"


def _is_ignored_skill_asset(path: Path) -> bool:
    """Ignore macOS metadata files and other hidden filesystem entries."""
    name = path.name
    return name.startswith(".")


def _parse_frontmatter(content: str) -> tuple[dict[str, object], str]:
    text = content.strip()
    if not text.startswith("---"):
        return {}, content

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content

    body_end = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body_end = index
            break
    if body_end is None:
        return {}, content
    frontmatter_lines = lines[1:body_end]
    metadata = _parse_simple_yaml_block(frontmatter_lines)
    return metadata, "\n".join(lines[body_end + 1:]).lstrip()


def _parse_simple_yaml_block(lines: list[str]) -> dict[str, object]:
    root: dict[str, object] = {}
    stack: list[tuple[int, object]] = [(-1, root)]

    def parse_scalar(raw: str) -> object:
        text = raw.strip()
        if not text:
            return ""
        if text[0] == text[-1] and text[0] in {"'", '"'}:
            return text[1:-1]
        lowered = text.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in {"null", "none"}:
            return None
        if re.fullmatch(r"-?\d+", text):
            try:
                return int(text)
            except Exception:
                return text
        return text

    def peek_next_meaningful(start: int) -> tuple[int, str] | None:
        for idx in range(start, len(lines)):
            stripped = lines[idx].strip()
            if not stripped or stripped.startswith("#"):
                continue
            return idx, lines[idx]
        return None

    index = 0
    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        container = stack[-1][1]

        if stripped.startswith("- "):
            if not isinstance(container, list):
                index += 1
                continue
            item_text = stripped[2:].strip()
            if re.match(r"^[^:]+:\s", item_text):
                key, value = item_text.split(":", 1)
                item: dict[str, object] = {key.strip(): parse_scalar(value)}
                container.append(item)
                stack.append((indent, item))
            else:
                container.append(parse_scalar(item_text))
            index += 1
            continue

        if ":" not in stripped or not isinstance(container, dict):
            index += 1
            continue

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            container[key] = parse_scalar(value)
            index += 1
            continue

        next_info = peek_next_meaningful(index + 1)
        if next_info is None:
            container[key] = {}
            index += 1
            continue
        _next_index, next_line = next_info
        next_indent = len(next_line) - len(next_line.lstrip(" "))
        next_stripped = next_line.strip()
        if next_indent <= indent:
            container[key] = {}
            index += 1
            continue
        child: object = [] if next_stripped.startswith("- ") else {}
        container[key] = child
        stack.append((indent, child))
        index += 1

    return root


class SkillLoader:
    def __init__(self, root: Path | None = None):
        self.root = root or SKILLS_ROOT

    @functools.lru_cache(maxsize=1)
    def load_all(self) -> tuple[SkillSpec, ...]:
        skills: list[SkillSpec] = []
        if not self.root.exists():
            return ()

        for skill_md in sorted(self.root.glob("*/SKILL.md")):
            if _is_ignored_skill_asset(skill_md):
                continue
            raw = skill_md.read_text(encoding="utf-8")
            metadata, _ = _parse_frontmatter(raw)
            phase = SkillPhase(metadata.get("phase", skill_md.parent.name))
            manifest = metadata
            refs_dir = skill_md.parent / "references"
            references = tuple(
                SkillReference(name=path.name, path=path)
                for path in sorted(refs_dir.iterdir())
                if path.is_file() and not _is_ignored_skill_asset(path)
            ) if refs_dir.exists() else ()
            assets = self._build_assets(
                root=skill_md.parent,
                skill_md=skill_md,
                manifest=manifest,
            )
            skills.append(
                SkillSpec(
                    name=metadata.get("name", skill_md.parent.name),
                    description=metadata.get("description", ""),
                    phase=phase,
                    order=int(metadata.get("order", "100")),
                    root=skill_md.parent,
                    skill_md=skill_md,
                    references=references,
                    assets=assets,
                    metadata_path=skill_md,
                )
            )
        skills.sort(key=lambda item: (item.order, item.name))
        return tuple(skills)

    @functools.lru_cache(maxsize=32)
    def get_skill(self, name: str) -> SkillSpec:
        for skill in self.load_all():
            if skill.name == name or skill.root.name == name:
                return skill
        raise KeyError(f"Unknown skill: {name}")

    def read_skill_body(self, name: str) -> str:
        spec = self.get_skill(name)
        _, body = _parse_frontmatter(spec.skill_md.read_text(encoding="utf-8"))
        return body

    @functools.lru_cache(maxsize=128)
    def read_reference(self, skill_name: str, reference_name: str) -> str:
        spec = self.get_skill(skill_name)
        ref_path = spec.root / "references" / reference_name
        if not ref_path.exists():
            raise FileNotFoundError(f"Reference not found: {skill_name}/{reference_name}")
        return ref_path.read_text(encoding="utf-8")

    def references_for_phase(self, phase: SkillPhase) -> tuple[SkillSpec, ...]:
        return tuple(skill for skill in self.load_all() if skill.phase == phase)

    def asset_registry(self) -> list[dict[str, object]]:
        registry: list[dict[str, object]] = []
        for skill in self.load_all():
            for asset in skill.assets:
                registry.append(
                    {
                        "skill": skill.name,
                        "phase": skill.phase.value,
                        "name": asset.name,
                        "kind": asset.kind.value,
                        "path": str(asset.path),
                        "prompt_mode": asset.prompt_mode,
                        "purpose": asset.purpose,
                        "injection_points": list(asset.injection_points),
                        "variables": list(asset.variables),
                        "tags": list(asset.tags),
                    }
                )
        return registry

    def _build_assets(
        self,
        *,
        root: Path,
        skill_md: Path,
        manifest: dict[str, object],
    ) -> tuple[SkillAssetSpec, ...]:
        assets: list[SkillAssetSpec] = []
        assets.append(
            self._make_asset(
                path=skill_md,
                kind=SkillAssetKind.SKILL_DOC,
                root=root,
                manifest=manifest,
            )
        )
        for dirname, kind in (("templates", SkillAssetKind.TEMPLATE), ("references", SkillAssetKind.REFERENCE)):
            base = root / dirname
            if not base.exists():
                continue
            for path in sorted(base.iterdir()):
                if not path.is_file() or _is_ignored_skill_asset(path):
                    continue
                assets.append(
                    self._make_asset(
                        path=path,
                        kind=kind,
                        root=root,
                        manifest=manifest,
                    )
                )
        return tuple(assets)

    def _make_asset(
        self,
        *,
        path: Path,
        kind: SkillAssetKind,
        root: Path,
        manifest: dict[str, object],
    ) -> SkillAssetSpec:
        relative = path.relative_to(root).as_posix()
        matched = self._match_manifest_entry(relative=relative, path=path, kind=kind, manifest=manifest)
        content = path.read_text(encoding="utf-8")
        variables = tuple(sorted(set(re.findall(r"\{([a-zA-Z0-9_]+)\}", content))))
        purpose = str(matched.get("purpose", "")).strip()
        injection_points = tuple(
            str(item).strip()
            for item in (matched.get("injection_points", []) or [])
            if str(item).strip()
        )
        tags = tuple(str(item).strip() for item in (matched.get("tags", []) or []) if str(item).strip())
        prompt_mode = str(
            matched.get(
                "prompt_mode",
                manifest.get("default_prompt_mode", "static"),
            )
        ).strip() or "static"
        return SkillAssetSpec(
            name=path.name,
            kind=kind,
            path=path,
            prompt_mode=prompt_mode,
            purpose=purpose,
            injection_points=injection_points,
            variables=variables,
            tags=tags,
        )

    @staticmethod
    def _match_manifest_entry(
        *,
        relative: str,
        path: Path,
        kind: SkillAssetKind,
        manifest: dict[str, object],
    ) -> dict[str, object]:
        groups = manifest.get("asset_groups", [])
        if not isinstance(groups, list):
            return {}
        candidate_relatives = {relative, path.name}
        candidate_kind = kind.value
        for item in groups:
            if not isinstance(item, dict):
                continue
            raw_kind = str(item.get("kind", "")).strip()
            if raw_kind and raw_kind != candidate_kind:
                continue
            pattern = str(item.get("pattern", "")).strip()
            if not pattern:
                continue
            if any(fnmatch.fnmatch(value, pattern) for value in candidate_relatives):
                return item
        return {}
