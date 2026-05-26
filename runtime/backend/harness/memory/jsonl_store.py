from __future__ import annotations

import logging
from pathlib import Path

from backend.harness.memory.models import MemoryRecord, MemoryType, MemoryWriteResult
from backend.harness.memory.namespace import namespace_to_filename, validate_namespace
from backend.harness.memory.safety import (
    sanitize_memory_artifacts,
    sanitize_memory_list,
    sanitize_memory_mapping,
    sanitize_memory_text,
)

logger = logging.getLogger(__name__)


class JsonlMemoryStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def write(self, record: MemoryRecord) -> MemoryWriteResult:
        safe_record = _sanitize_record(record)
        records = self._load_file_records(self._record_path(safe_record.namespace, safe_record.memory_type))
        updated = False
        for index, existing in enumerate(records):
            if existing.memory_id == safe_record.memory_id:
                records[index] = safe_record
                updated = True
                break
        if not updated:
            records.append(safe_record)
        self._save_file_records(self._record_path(safe_record.namespace, safe_record.memory_type), records)
        return MemoryWriteResult(memory_id=safe_record.memory_id, created=not updated, updated=updated)

    def get(self, memory_id: str) -> MemoryRecord | None:
        for record in self.list_records():
            if record.memory_id == memory_id:
                return record
        return None

    def list_records(
        self,
        *,
        namespace: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryRecord]:
        if namespace is not None:
            validate_namespace(namespace)
        records: list[MemoryRecord] = []
        for path in self._candidate_paths(namespace=namespace, memory_type=memory_type):
            records.extend(self._load_file_records(path))
        return records

    def update(self, record: MemoryRecord) -> MemoryWriteResult:
        existing = self.get(record.memory_id)
        if existing is None:
            return MemoryWriteResult(memory_id=record.memory_id, created=False, skipped=True, reason="memory not found")
        if existing.namespace != record.namespace or existing.memory_type != record.memory_type:
            return MemoryWriteResult(
                memory_id=record.memory_id,
                created=False,
                skipped=True,
                reason="memory namespace or type change is not supported",
            )
        return self.write(record)

    def _record_path(self, namespace: str, memory_type: MemoryType) -> Path:
        filename = namespace_to_filename(namespace)
        return self.root / memory_type.value / f"{filename}.jsonl"

    def _candidate_paths(self, *, namespace: str | None, memory_type: MemoryType | None) -> list[Path]:
        if namespace and memory_type:
            return [self._record_path(namespace, memory_type)]
        type_dirs = [self.root / memory_type.value] if memory_type else [path for path in self.root.glob("*") if path.is_dir()]
        paths: list[Path] = []
        for type_dir in type_dirs:
            if namespace:
                try:
                    paths.append(type_dir / f"{namespace_to_filename(namespace)}.jsonl")
                except ValueError:
                    return []
            else:
                paths.extend(sorted(type_dir.glob("*.jsonl")))
        return paths

    def _load_file_records(self, path: Path) -> list[MemoryRecord]:
        if not path.exists():
            return []
        records: list[MemoryRecord] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            logger.warning("[Memory] Failed to read memory file %s: %s", path, exc)
            return []
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(MemoryRecord.model_validate_json(line))
            except Exception:
                continue
        return records

    def _save_file_records(self, path: Path, records: list[MemoryRecord]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(record.model_dump_json() for record in records)
        path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")


def _sanitize_record(record: MemoryRecord) -> MemoryRecord:
    return record.model_copy(
        update={
            "namespace": validate_namespace(record.namespace),
            "key": sanitize_memory_text(record.key, limit=200),
            "content": sanitize_memory_text(record.content),
            "context": sanitize_memory_mapping(record.context),
            "outcome": sanitize_memory_mapping(record.outcome),
            "tags": sanitize_memory_list(record.tags),
            "source_artifacts": sanitize_memory_artifacts(record.source_artifacts),
        }
    )
