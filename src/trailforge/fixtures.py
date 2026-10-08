"""Bounded standalone fixture loader; never executes repository code."""

from __future__ import annotations

from pathlib import Path

from trailforge.core import (CONFIG, VERSION, ContractError, ReviewInput, ScopeBinding,
                            byte_digest, digest, fields, nonempty, source_path, strict_json)


def _bounded_bytes(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise ContractError("fixture file unavailable") from exc
    if len(raw) > limit:
        raise ContractError("fixture exceeds local byte limit")
    return raw


def _safe_file(root: Path, relative: str) -> Path:
    source_path(relative)
    path = root
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ContractError("fixture links/junctions are forbidden")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ContractError("fixture path is not a regular scoped file")
    return path


def load_fixture(directory: str | Path) -> ReviewInput:
    directory = Path(directory)
    if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
        raise ContractError("fixture root cannot be a link/junction")
    root = directory.resolve()
    raw_manifest = _bounded_bytes(_safe_file(root, "fixture.json"), CONFIG["max_manifest_bytes"])
    try:
        manifest = strict_json(raw_manifest.decode("utf-8"))
        fields(manifest, {"schema_version", "tenant_id", "resource_id", "base_revision", "head_revision", "files", "mock_output"})
        if manifest["schema_version"] != VERSION:
            raise ContractError("unsupported fixture schema version")
        for key in ("tenant_id", "resource_id", "base_revision", "head_revision"):
            nonempty(manifest[key])
        if type(manifest["files"]) is not list or not 1 <= len(manifest["files"]) <= CONFIG["max_files"]:
            raise ContractError("invalid fixture file count")
        fields(manifest["mock_output"], {"findings", "roles"})
        sources, seen, total = [], set(), 0
        for spec in manifest["files"]:
            fields(spec, {"path", "truncated"})
            path = source_path(spec["path"])
            if path in seen or type(spec["truncated"]) is not bool:
                raise ContractError("duplicate path or invalid truncation flag")
            seen.add(path)
            raw = _bounded_bytes(_safe_file(root, path), CONFIG["max_file_bytes"])
            total += len(raw)
            if total > CONFIG["max_source_bytes"]:
                raise ContractError("fixture exceeds combined source limit")
            language = {".py": "python", ".js": "javascript", ".ts": "typescript"}.get(Path(path).suffix, "unsupported")
            sources.append({"path": path, "language": language, "content": raw.decode("utf-8"),
                            "content_digest": byte_digest(raw), "truncated": spec["truncated"]})
    except UnicodeError as exc:
        raise ContractError("fixture must be UTF-8") from exc
    binding = ScopeBinding(manifest["tenant_id"], manifest["resource_id"], manifest["base_revision"],
                           manifest["head_revision"], digest({"schema_version": VERSION, "sources": sources,
                                                               "mock_output": manifest["mock_output"]}))
    return ReviewInput(binding, tuple(sources), manifest["mock_output"])
