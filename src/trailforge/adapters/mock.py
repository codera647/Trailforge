"""Fixture-configured mock. It does not infer bugs or measure model quality."""

from __future__ import annotations

from copy import deepcopy

from trailforge.core import CONFIG, ROLES, ContractError, fields


class FixtureModel:
    adapter_id = "trailforge.fixture_mock/0.1"

    def review(self, snapshot: dict, configured_output: dict) -> dict:
        output = deepcopy(configured_output)
        fields(output, {"findings", "roles"})
        if type(output["findings"]) is not list or len(output["findings"]) > CONFIG["max_findings"]:
            raise ContractError("invalid/over-limit mock findings")
        fields(output["roles"], set(ROLES))
        artifacts = {a["path"]: a for a in snapshot["artifacts"]}
        for proposal in output["findings"]:
            # Fill only absent reference metadata, preserving forged values for rejection.
            if type(proposal) is not dict or type(proposal.get("evidence")) is not dict:
                continue
            ref = proposal["evidence"]
            artifact = artifacts.get(ref.get("path")) if type(ref.get("path")) is str else None
            if artifact:
                ref.setdefault("revision", artifact["receipt"]["revision"])
                ref.setdefault("content_digest", artifact["content_digest"])
                ref.setdefault("receipt_id", artifact["receipt"]["receipt_id"])
        return output
