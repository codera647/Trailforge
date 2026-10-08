"""Closed local action policy. Proposals carry data, never credentials or authority."""
from dataclasses import dataclass, field

from .contracts import PROTOCOL_VERSION, hash_value, integer
from .core import ContractError, canonical, digest, fields, nonempty, source_path

BROKER_POLICY = {"profile": "local-review/0.1", "tool_limit": 64,
                 "session_seconds": 3600, "pending_seconds": 900, "approval_seconds": 600}
ROLES = frozenset({"OPERATOR", "WORKER", "HOST", "REVIEWER"})
ACTION_ROLES = {"READ_SOURCE": {"WORKER"}, "MOCK_REVIEW": {"WORKER"},
                "REQUEST_SIMULATION": {"HOST"}, "SIMULATE_COMMENT": {"HOST"}}


class AdmissionDenied(ContractError):
    """Stable public denial without attacker text, tokens or provider details."""

    def __init__(self, code="ADMISSION_DENIED"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LocalSession:
    secret: str = field(repr=False)


def policy_digest(tenant_id, resource_id, version, enabled):
    return digest({"tenant_id": tenant_id, "resource_id": resource_id,
                   "version": version, "enabled": enabled, "policy": BROKER_POLICY})


def validate_proposal(proposal):
    try:
        fields(proposal, {"schema_version", "run_id", "binding_digest", "policy_digest",
                          "expected_version", "action", "arguments"})
        if len(canonical(proposal).encode("utf-8")) > 65536:
            raise ContractError("proposal too large")
        if proposal["schema_version"] != PROTOCOL_VERSION:
            raise ContractError("unsupported proposal version")
        nonempty(proposal["run_id"])
        hash_value(proposal["binding_digest"])
        hash_value(proposal["policy_digest"])
        integer(proposal["expected_version"])
        action = proposal["action"]
        if type(action) is not str or action not in ACTION_ROLES:
            raise ContractError("unsupported action")
        args = proposal["arguments"]
        required = {"READ_SOURCE": {"path"}, "MOCK_REVIEW": set(),
                    "REQUEST_SIMULATION": {"draft_digest"}, "SIMULATE_COMMENT": {"request_id"}}[action]
        fields(args, required)
        if action == "READ_SOURCE":
            source_path(args["path"])
        elif action == "REQUEST_SIMULATION":
            hash_value(args["draft_digest"])
        elif action == "SIMULATE_COMMENT":
            nonempty(args["request_id"])
        return proposal
    except (ContractError, TypeError, ValueError, KeyError):
        raise AdmissionDenied("INVALID_PROPOSAL") from None


def authorize(proposal, record, roles, current_policy, observed_head):
    validate_proposal(proposal)
    if not ACTION_ROLES[proposal["action"]].intersection(roles):
        raise AdmissionDenied("ROLE_DENIED")
    if proposal["run_id"] != record["run_id"] or proposal["binding_digest"] != digest(record["binding"]):
        raise AdmissionDenied("BINDING_CHANGED")
    if proposal["policy_digest"] != current_policy:
        raise AdmissionDenied("POLICY_CHANGED")
    if proposal["expected_version"] != record["state_version"]:
        raise AdmissionDenied("STATE_CHANGED")
    if observed_head != record["binding"]["head_revision"]:
        raise AdmissionDenied("REVISION_CHANGED")
    action = proposal["action"]
    if action == "READ_SOURCE" and record["snapshot"] is None:
        raise AdmissionDenied("SNAPSHOT_REQUIRED")
    if action == "MOCK_REVIEW" and record["stage"] != "SNAPSHOT":
        raise AdmissionDenied("STAGE_DENIED")
    if action in ("REQUEST_SIMULATION", "SIMULATE_COMMENT") and record["stage"] != "DRAFTED":
        raise AdmissionDenied("DRAFT_REQUIRED")
    if action == "REQUEST_SIMULATION" and proposal["arguments"]["draft_digest"] != record["draft"]["draft_digest"]:
        raise AdmissionDenied("PAYLOAD_CHANGED")


def comment_preview(record):
    """Exact reviewable payload derived only from a validated durable draft."""
    draft = record["draft"]
    return {"schema_version": PROTOCOL_VERSION, "kind": "LOCAL_COMMENT_SIMULATION",
            "run_id": record["run_id"], "binding_digest": digest(record["binding"]),
            "head_revision": record["binding"]["head_revision"], "draft_digest": draft["draft_digest"],
            "body": canonical(draft), "external_effect": "NONE"}


def action_proposal(record, current_policy, action, arguments):
    return {"schema_version": PROTOCOL_VERSION, "run_id": record["run_id"],
            "binding_digest": digest(record["binding"]), "policy_digest": current_policy,
            "expected_version": record["state_version"], "action": action, "arguments": arguments}
