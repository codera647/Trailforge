"""Strict local protocol. This is not the full proposed production schema."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

VERSION = "offline/0.1"
ROLES = ("security", "correctness", "tests", "documentation")
POLICY = {"mode": "DRAFT", "required_roles": list(ROLES), "max_model_calls": 2}
CONFIG = {"max_files": 32, "max_file_bytes": 65536, "max_source_bytes": 262144,
          "max_manifest_bytes": 65536, "max_findings": 64}


class ContractError(ValueError):
    """A proposal/input violates the local contract; no silent coercion."""


class ConflictError(ContractError):
    """A checkpoint write uses a stale state version."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def byte_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def strict_json(raw: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ContractError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ContractError("nonfinite JSON number")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, TypeError) as exc:
        raise ContractError("invalid JSON") from exc


def fields(value: Any, required: set[str], optional: set[str] | None = None) -> dict:
    if type(value) is not dict or not required <= value.keys() or value.keys() - required - (optional or set()):
        raise ContractError("missing or unknown fields")
    return value


def nonempty(value: Any) -> str:
    if type(value) is not str or not value.strip():
        raise ContractError("expected nonempty string")
    return value


def source_path(value: Any) -> str:
    nonempty(value)
    if any(c in value for c in ("\\", ":", "\x00")) or value.startswith("/"):
        raise ContractError("source path must be canonical relative POSIX path")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ContractError("source path traversal/noncanonical path")
    return value


@dataclass(frozen=True)
class ScopeBinding:
    tenant_id: str
    resource_id: str
    base_revision: str
    head_revision: str
    input_digest: str
    policy_digest: str = digest(POLICY)
    profile_version: str = VERSION
    config_digest: str = digest(CONFIG)

    def as_dict(self) -> dict[str, str]:
        return dict(vars(self))

    @property
    def binding_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True)
class ReviewInput:
    binding: ScopeBinding
    sources: tuple[dict, ...]
    mock_output: dict

    @property
    def computed_input_digest(self) -> str:
        return digest({"schema_version": VERSION, "sources": list(self.sources), "mock_output": self.mock_output})


class ModelAdapter(Protocol):
    adapter_id: str

    def review(self, snapshot: dict, configured_output: dict) -> dict: ...


class RunStore(Protocol):
    def create(self, record: dict) -> None: ...
    def load(self, run_id: str) -> dict: ...
    def save(self, record: dict, expected_version: int, event: str) -> dict: ...
