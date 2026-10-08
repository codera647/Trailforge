"""Bounded deterministic offline workflow, with persisted recovery stages."""

from __future__ import annotations

from uuid import uuid4

from .core import CONFIG, POLICY, VERSION, ContractError, ModelAdapter, ReviewInput, RunStore, digest, fields
from .evidence import capture, validate_output, verify_snapshot
from .contracts import make_draft

STATES = {"ADMITTED": "READY", "SNAPSHOT": "RUNNING", "REVIEWED": "CHECKING",
          "VALIDATED": "CHECKING", "DRAFTED": "TERMINAL"}


class OfflineController:
    def __init__(self, store: RunStore, model: ModelAdapter):
        self.store, self.model = store, model

    def start(self, review_input: ReviewInput) -> str:
        record = self.initial_record(review_input)
        self.store.create(record)
        return record["run_id"]

    @classmethod
    def initial_record(cls, review_input: ReviewInput) -> dict:
        """Construct admission for a trusted host's larger atomic transaction."""
        binding = review_input.binding.as_dict()
        if review_input.computed_input_digest != binding["input_digest"]:
            raise ContractError("input content does not match immutable binding")
        cls._check_binding(binding)
        run_id = str(uuid4())
        record = {"schema_version": VERSION, "run_id": run_id, "binding": binding,
                  "state_version": 0, "state": "READY", "stage": "ADMITTED",
                  "outcome": None, "model_calls": 0, "snapshot": None,
                  "review": None, "validation": None, "draft": None}
        return record

    @staticmethod
    def _check_binding(binding: dict) -> None:
        if (binding["profile_version"] != VERSION or binding["policy_digest"] != digest(POLICY)
                or binding["config_digest"] != digest(CONFIG)):
            raise ContractError("unsupported profile/policy/config binding")

    def _save(self, record: dict, event: str) -> dict:
        return self.store.save(record, record["state_version"], event)

    def _verify(self, record: dict, review_input: ReviewInput) -> None:
        fields(record, {"schema_version", "run_id", "binding", "state_version", "state", "stage", "outcome",
                        "model_calls", "snapshot", "review", "validation", "draft"})
        if record["binding"] != review_input.binding.as_dict():
            raise ContractError("resume tenant/resource/revision/input binding mismatch")
        if review_input.computed_input_digest != record["binding"]["input_digest"]:
            raise ContractError("input content does not match immutable binding")
        self._check_binding(record["binding"])
        stage = record["stage"]
        if stage not in STATES or record["state"] != STATES[stage]:
            raise ContractError("invalid state/stage transition")
        if type(record["model_calls"]) is not int or not 0 <= record["model_calls"] <= POLICY["max_model_calls"]:
            raise ContractError("invalid persisted allowance")
        if stage != "ADMITTED":
            verify_snapshot(record["snapshot"], record["run_id"], record["binding"])
            expected = capture(record["run_id"], record["binding"], review_input.sources)
            if record["snapshot"] != expected:
                raise ContractError("snapshot differs from admitted fixture")
        if stage in ("REVIEWED", "VALIDATED", "DRAFTED"):
            if record["model_calls"] < 1:
                raise ContractError("missing persisted model invocation")
            review = fields(record["review"], {"adapter_id", "output", "digest"})
            if review["adapter_id"] != self.model.adapter_id or review["digest"] != digest(review["output"]):
                raise ContractError("review adapter/output changed")
        if stage in ("VALIDATED", "DRAFTED"):
            expected_validation = validate_output(record["review"]["output"], record["snapshot"],
                                                  record["run_id"], record["binding"])
            if expected_validation != record["validation"]:
                raise ContractError("validation no longer matches evidence")
        if stage == "DRAFTED":
            expected_draft = self._draft(record)
            if record["draft"] != expected_draft or record["outcome"] != expected_draft["workflow_outcome"]:
                raise ContractError("draft/outcome changed")
        elif record["outcome"] is not None:
            raise ContractError("nonterminal run has outcome")

    @staticmethod
    def _draft(record: dict) -> dict:
        return make_draft(record)

    def advance(self, run_id: str, review_input: ReviewInput, pause_after: str | None = None) -> dict:
        # A finished resume is read-only. Distributed stores wrap mutation in a
        # live lease facade; the model only receives the source snapshot.
        if hasattr(self.store, "worker"):
            record = self.store.load(run_id)
            self._verify(record, review_input)
            if record["stage"] == "DRAFTED":
                return record
            with self.store.worker(run_id) as leased_store:
                OfflineController(leased_store, self.model).advance(run_id, review_input, pause_after)
            return self.store.load(run_id)
        if pause_after not in (None, "snapshot", "review", "validation"):
            raise ContractError("unknown pause checkpoint")
        record = self.store.load(run_id)
        self._verify(record, review_input)
        if record["stage"] == "ADMITTED":
            record.update(snapshot=capture(run_id, record["binding"], review_input.sources),
                          stage="SNAPSHOT", state="RUNNING")
            record = self._save(record, "SNAPSHOT_CAPTURED")
            if pause_after == "snapshot":
                return record
        if record["stage"] == "SNAPSHOT":
            if record["model_calls"] >= POLICY["max_model_calls"]:
                raise ContractError("mock invocation allowance exhausted; no reset on resume")
            if hasattr(self.store, "admit_model"):
                record = self.store.admit_model(record)
            else:
                record["model_calls"] += 1
                record = self._save(record, "MOCK_INVOCATION_ADMITTED")
            # A crash here consumes this attempt. No external effect is possible.
            output = self.model.review(record["snapshot"], review_input.mock_output)
            record.update(review={"adapter_id": self.model.adapter_id, "output": output, "digest": digest(output)},
                          stage="REVIEWED", state="CHECKING")
            record = self._save(record, "MOCK_REVIEW_RECORDED")
            if pause_after == "review":
                return record
        if record["stage"] == "REVIEWED":
            record.update(validation=validate_output(record["review"]["output"], record["snapshot"],
                                                     run_id, record["binding"]), stage="VALIDATED")
            record = self._save(record, "EVIDENCE_VALIDATED")
            if pause_after == "validation":
                return record
        if record["stage"] == "VALIDATED":
            draft = self._draft(record)
            record.update(draft=draft, stage="DRAFTED", state="TERMINAL", outcome=draft["workflow_outcome"])
            record = self._save(record, "DRAFT_SAVED")
        return record
