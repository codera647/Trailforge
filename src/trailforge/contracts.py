"""Deterministic semantic rules for the current checkpoint protocol.

Public record schemas describe shapes. They never grant access or mint approvals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from .core import CONFIG, POLICY, ROLES, VERSION, ContractError, digest, fields, nonempty

PROTOCOL_VERSION = "trailforge/0.1"
STAGE_STATE = {"ADMITTED": "READY", "SNAPSHOT": "RUNNING", "REVIEWED": "CHECKING",
               "VALIDATED": "CHECKING", "DRAFTED": "TERMINAL"}
STAGE_ORDER = tuple(STAGE_STATE)


class LeaseError(ContractError):
    """Expired/stale worker has no mutation authority."""


class BudgetError(ContractError):
    """No available units in at least one authoritative scope."""


@dataclass(frozen=True)
class LeaseToken:
    run_id: str
    worker_id: str
    epoch: int


def integer(value, minimum=0, maximum=2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError("expected bounded integer (booleans are not integers)")
    return value


def hash_value(value) -> str:
    if type(value) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ContractError("invalid sha256 digest")
    return value


def validate_binding(binding: dict) -> None:
    fields(binding, {"tenant_id", "resource_id", "base_revision", "head_revision", "input_digest",
                     "policy_digest", "profile_version", "config_digest"})
    for name in ("tenant_id", "resource_id", "base_revision", "head_revision"):
        nonempty(binding[name])
    for name in ("input_digest", "policy_digest", "config_digest"):
        hash_value(binding[name])
    if (binding["profile_version"] != VERSION or binding["policy_digest"] != digest(POLICY)
            or binding["config_digest"] != digest(CONFIG)):
        raise ContractError("unsupported profile/policy/config binding")


def validate_checkpoint(record: dict) -> None:
    fields(record, {"schema_version", "run_id", "binding", "state_version", "state", "stage", "outcome",
                    "model_calls", "snapshot", "review", "validation", "draft"})
    if record["schema_version"] != VERSION:
        raise ContractError("unsupported checkpoint schema version")
    nonempty(record["run_id"])
    validate_binding(record["binding"])
    integer(record["state_version"])
    integer(record["model_calls"], maximum=POLICY["max_model_calls"])
    stage = record["stage"]
    if type(stage) is not str or stage not in STAGE_STATE or record["state"] != STAGE_STATE[stage]:
        raise ContractError("invalid state/stage transition")
    index = STAGE_ORDER.index(stage)
    for name, required_at in (("snapshot", 1), ("review", 2), ("validation", 3), ("draft", 4)):
        if index >= required_at:
            if type(record[name]) is not dict:
                raise ContractError("missing required checkpoint artifact")
        elif record[name] is not None:
            raise ContractError("artifact exists before its required stage")
    if index >= 2 and record["model_calls"] < 1:
        raise ContractError("review lacks admitted model call")
    if index == 4:
        if record["outcome"] not in ("COMPLETE", "PARTIAL"):
            raise ContractError("invalid terminal outcome")
    elif record["outcome"] is not None:
        raise ContractError("nonterminal run has outcome")


def validate_transition(previous: dict, candidate: dict, event: str) -> None:
    validate_checkpoint(previous)
    validate_checkpoint(candidate)
    nonempty(event)
    if (candidate["run_id"] != previous["run_id"] or candidate["binding"] != previous["binding"]
            or candidate["schema_version"] != previous["schema_version"]
            or candidate["state_version"] != previous["state_version"]):
        raise ContractError("transition changed immutable identity/version")
    before, after = STAGE_ORDER.index(previous["stage"]), STAGE_ORDER.index(candidate["stage"])
    if after not in (before, before + 1) or (before == 4 and candidate != previous):
        raise ContractError("invalid skipped/backwards/terminal transition")
    if candidate["model_calls"] < previous["model_calls"]:
        raise ContractError("transition reset consumed allowance")
    for name in ("snapshot", "review", "validation", "draft"):
        if previous[name] is not None and candidate[name] != previous[name]:
            raise ContractError("transition rewrote a recorded artifact")
    # Only the database admission operation may raise this counter.
    if candidate["model_calls"] != previous["model_calls"] and event != "MOCK_INVOCATION_ADMITTED":
        raise ContractError("model units require atomic reservation")
    expected = {("ADMITTED", "SNAPSHOT"): "SNAPSHOT_CAPTURED",
                ("SNAPSHOT", "REVIEWED"): "MOCK_REVIEW_RECORDED",
                ("REVIEWED", "VALIDATED"): "EVIDENCE_VALIDATED",
                ("VALIDATED", "DRAFTED"): "DRAFT_SAVED"}
    if before != after and event != expected[(previous["stage"], candidate["stage"])]:
        raise ContractError("event does not match transition")


def validate_roles(roles: dict) -> None:
    fields(roles, set(ROLES))
    if any(status not in ("COMPLETE", "TIMEOUT", "FAILED", "SKIPPED") for status in roles.values()):
        raise ContractError("invalid role result")


def make_draft(record: dict) -> dict:
    validation = record["validation"]
    body = {"schema_version": VERSION, "run_id": record["run_id"],
            "binding": record["binding"], "mode": "DRAFT", "reviewer": "MOCK",
            "workflow_outcome": "PARTIAL" if validation["coverage"]["gaps"] else "COMPLETE",
            **validation, "model_calls": record["model_calls"],
            "notice": "Offline mock draft. COMPLETE describes this workflow only; no independent semantic verification or production quality evaluation."}
    return {**body, "draft_digest": digest(body)}


def validate_artifacts(record: dict) -> None:
    """Recorded validation/draft cannot override exact source/coverage checks."""
    from .evidence import validate_output, verify_snapshot

    validate_checkpoint(record)
    index = STAGE_ORDER.index(record["stage"])
    if index >= 1:
        verify_snapshot(record["snapshot"], record["run_id"], record["binding"])
    if index >= 2:
        fields(record["review"], {"adapter_id", "output", "digest"})
        nonempty(record["review"]["adapter_id"])
        if record["review"]["digest"] != digest(record["review"]["output"]):
            raise ContractError("review output digest mismatch")
        fields(record["review"]["output"], {"findings", "roles"})
        validate_roles(record["review"]["output"]["roles"])
    if index >= 3:
        expected = validate_output(record["review"]["output"], record["snapshot"], record["run_id"], record["binding"])
        if record["validation"] != expected:
            raise ContractError("recorded validation differs from source/coverage")
    if index == 4:
        if record["draft"] != make_draft(record) or record["outcome"] != record["draft"]["workflow_outcome"]:
            raise ContractError("draft does not match recorded validation/outcome")


def assert_approval_binding(approval: dict, binding_digest: str, payload_digest: str,
                            policy_digest: str, action: str, now: datetime, *, run_id: str) -> None:
    """Validate exact binding/expiry only; trusted actor issuance is a separate gate."""
    fields(approval, {"schema_version", "approval_id", "actor_id", "run_id", "binding_digest",
                      "payload_digest", "policy_digest", "action", "decision", "expires_at"})
    if approval["schema_version"] != PROTOCOL_VERSION:
        raise ContractError("unsupported approval version")
    for name in ("approval_id", "actor_id", "run_id"):
        nonempty(approval[name])
    if (approval["run_id"] != run_id or approval["binding_digest"] != binding_digest or approval["payload_digest"] != payload_digest
            or approval["policy_digest"] != policy_digest or approval["action"] != action
            or approval["decision"] != "APPROVE"):
        raise ContractError("approval action/payload/binding mismatch")
    try:
        expiry = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContractError("invalid approval expiry") from exc
    if expiry.tzinfo is None or now.tzinfo is None or expiry <= now.astimezone(timezone.utc):
        raise ContractError("expired or timezone-naive approval")
