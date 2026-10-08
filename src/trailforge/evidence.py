"""Source integrity checks. Matching evidence does not establish diagnosis truth."""

from __future__ import annotations

from .core import CONFIG, POLICY, ROLES, ContractError, byte_digest, digest, fields, nonempty, source_path


def capture(run_id: str, binding: dict, sources: tuple[dict, ...]) -> dict:
    artifacts = []
    for source in sources:
        artifact = {**source, "trust": "UNTRUSTED_SOURCE"}
        receipt_body = {"issuer": "trailforge.local_snapshot/0.1", "run_id": run_id,
                        "binding_digest": digest(binding), "path": source["path"],
                        "content_digest": source["content_digest"],
                        "revision": binding["head_revision"]}
        artifact["receipt"] = {**receipt_body, "receipt_id": digest(receipt_body)}
        artifacts.append(artifact)
    return {"artifacts": artifacts, "digest": digest(artifacts)}


def verify_snapshot(snapshot: dict, run_id: str, binding: dict) -> None:
    fields(snapshot, {"artifacts", "digest"})
    if digest(snapshot["artifacts"]) != snapshot["digest"]:
        raise ContractError("snapshot manifest changed")
    paths = set()
    for artifact in snapshot["artifacts"]:
        fields(artifact, {"path", "language", "content", "content_digest", "truncated", "trust", "receipt"})
        path = source_path(artifact["path"])
        if path in paths or artifact["trust"] != "UNTRUSTED_SOURCE":
            raise ContractError("duplicate source or changed trust label")
        paths.add(path)
        if byte_digest(artifact["content"].encode("utf-8")) != artifact["content_digest"]:
            raise ContractError("source digest changed")
        receipt = artifact["receipt"]
        body = {"issuer": "trailforge.local_snapshot/0.1", "run_id": run_id,
                "binding_digest": digest(binding), "path": path,
                "content_digest": artifact["content_digest"], "revision": binding["head_revision"]}
        if receipt != {**body, "receipt_id": digest(body)}:
            raise ContractError("invalid snapshot receipt")


def validate_output(output: dict, snapshot: dict, run_id: str, binding: dict) -> dict:
    verify_snapshot(snapshot, run_id, binding)
    fields(output, {"findings", "roles"})
    if type(output["findings"]) is not list or len(output["findings"]) > CONFIG["max_findings"]:
        raise ContractError("invalid/over-limit findings")
    fields(output["roles"], set(ROLES))
    gaps = []
    roles = []
    for role in ROLES:
        status = output["roles"][role]
        if status not in ("COMPLETE", "TIMEOUT", "FAILED", "SKIPPED"):
            raise ContractError("invalid role status")
        roles.append({"role": role, "status": status, "method": "MOCK"})
        if status != "COMPLETE":
            gaps.append({"kind": "role", "role": role, "reason": status})
    artifacts = {a["path"]: a for a in snapshot["artifacts"]}
    inspected = []
    for path, artifact in artifacts.items():
        if artifact["language"] not in ("python", "javascript", "typescript"):
            gaps.append({"kind": "file", "path": path, "reason": "UNSUPPORTED_LANGUAGE"})
        elif artifact["truncated"]:
            gaps.append({"kind": "file", "path": path, "reason": "TRUNCATED_SOURCE"})
        else:
            inspected.append(path)
    admitted, rejected, fingerprints = [], [], set()
    for index, finding in enumerate(output["findings"]):
        try:
            fields(finding, {"title", "category", "severity", "scenario", "impact", "recommendation", "role", "evidence"})
            for key in ("title", "category", "scenario", "impact", "recommendation"):
                nonempty(finding[key])
            if finding["severity"] not in ("LOW", "MEDIUM", "HIGH", "CRITICAL") or finding["role"] not in ROLES:
                raise ContractError("invalid severity/role")
            if output["roles"][finding["role"]] != "COMPLETE":
                raise ContractError("finding from incomplete role")
            ref = fields(finding["evidence"], {"path", "start_line", "end_line", "quote", "revision", "content_digest", "receipt_id"})
            path = source_path(ref["path"])
            artifact = artifacts.get(path)
            if artifact is None or path not in inspected:
                raise ContractError("source unavailable or unsupported")
            if (ref["revision"] != binding["head_revision"] or ref["content_digest"] != artifact["content_digest"]
                    or ref["receipt_id"] != artifact["receipt"]["receipt_id"]):
                raise ContractError("stale or forged source reference")
            start, end = ref["start_line"], ref["end_line"]
            lines = artifact["content"].splitlines(keepends=True)
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
                raise ContractError("line range outside exact source")
            if ref["quote"] != "".join(lines[start - 1:end]):
                raise ContractError("quote differs from exact source")
            fingerprint = digest({"category": finding["category"], "scenario": finding["scenario"], "evidence": ref})
            if fingerprint in fingerprints:
                raise ContractError("duplicate finding")
            fingerprints.add(fingerprint)
            admitted.append({**finding, "finding_id": fingerprint, "run_id": run_id,
                             "binding_digest": digest(binding), "disposition": "DRAFT_ONLY",
                             "reliability": None, "critic": "NOT_IMPLEMENTED",
                             "needs_human_review": True})
        except ContractError as exc:
            rejected.append({"proposal_index": index, "reason": str(exc)})
    if rejected:
        gaps.append({"kind": "validation", "reason": "REJECTED_PROPOSALS", "count": len(rejected)})
    return {"findings": admitted, "rejected": rejected,
            "coverage": {"required_roles": POLICY["required_roles"], "roles": roles,
                         "inspected_files": inspected, "gaps": gaps,
                         "semantic_verification": "NOT_IMPLEMENTED"}}
