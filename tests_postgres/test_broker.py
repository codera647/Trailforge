"""Real Postgres authority, approval invalidation and atomicity boundaries."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import os
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
import psycopg
from psycopg import sql
from support import fixture as load_case, fixture_path, register_schema
from trailforge.adapters.mock import FixtureModel
from trailforge.adapters.broker import PostgresBroker
from trailforge.adapters.postgres import PostgresRunStore
from trailforge.adapters.schema import validate_record
from trailforge.broker import AdmissionDenied, LocalSession, action_proposal
from trailforge.contracts import BudgetError, LeaseError
from trailforge.controller import OfflineController
from trailforge.core import ContractError, digest


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["TRAILFORGE_DATABASE_URL"]
        self.schema = "tf_broker_test_" + uuid4().hex
        self.store = PostgresRunStore(self.dsn, "local-demo", "demo-repository", schema=self.schema)
        register_schema(self.schema)
        self.store.migrate()
        self.addCleanup(self.cleanup_schema)
        self.broker = PostgresBroker(self.store)
        self.operator = self.broker.bootstrap_operator("operator")
        self.worker = self.broker.grant_session(self.operator, "worker", ["WORKER"])
        self.host = self.broker.grant_session(self.operator, "host", ["HOST"])
        self.reviewer = self.broker.grant_session(self.operator, "reviewer", ["REVIEWER"])
        self.fixture = load_case("PY001")

    def cleanup_schema(self):
        assert self.schema.startswith("tf_broker_test_") and len(self.schema) == 47
        with psycopg.connect(self.dsn, connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=5000") as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def controller(self, session=None):
        return OfflineController(self.broker.controller_store(session or self.worker), FixtureModel())

    def start_case(self, pause=None):
        c = self.controller()
        run_id = c.start(self.fixture)
        return c.advance(run_id, self.fixture, pause)

    def proposal(self, record, action, args=None, session=None):
        return action_proposal(record, self.broker.context(session or self.host)["policy_digest"], action, args or {})

    def request(self, record=None):
        record = record or self.start_case()
        request = self.broker.execute(self.host, self.proposal(record, "REQUEST_SIMULATION", {"draft_digest": record["draft"]["draft_digest"]}))
        return record, request

    def approve(self, record, request, session=None, decision="APPROVE"):
        return self.broker.decide(session or self.reviewer, record["run_id"], request["request_id"],
                                  request["payload_digest"], request["policy_digest"], decision)

    def simulate(self, record, request, session=None):
        return self.broker.execute(session or self.host, self.proposal(record, "SIMULATE_COMMENT", {"request_id": request["request_id"]}, session=session))

    def expire_session(self, session):
        with self.store.connect() as db:
            db.execute("UPDATE broker_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE token_hash=%s", (self.broker._hash(session),))

    def test_brokered_controller_and_durable_idempotent_simulation(self):
        record, request = self.request()
        self.assertEqual(record["model_calls"], 1)
        self.assertEqual(request["status"], "PENDING_HUMAN")
        with self.assertRaises(AdmissionDenied):
            self.simulate(record, request)
        approval = self.approve(record, request)
        self.assertEqual(approval["actor_id"], "reviewer")
        receipt = self.simulate(record, request)
        fresh = PostgresBroker(PostgresRunStore(self.dsn, *self.broker.scope, schema=self.schema))
        self.assertEqual(fresh.execute(self.host, self.proposal(record, "SIMULATE_COMMENT", {"request_id": request["request_id"]})), receipt)
        self.assertEqual(receipt["external_effect"], "NONE")
        self.assertEqual(fresh.usage(self.host, record["run_id"]), 3)
        self.assertEqual(self.controller().advance(record["run_id"], self.fixture), record)
        self.assertEqual([r["charged"] for r in self.store.budget_usage(record["run_id"])], [1, 1, 1])

    def test_raw_capabilities_absent_from_database_and_outputs(self):
        record, request = self.request()
        with self.store.connect() as db:
            sessions = db.execute("SELECT * FROM broker_sessions").fetchall()
            audit = db.execute("SELECT * FROM broker_audit").fetchall()
        for session in (self.operator, self.worker, self.host, self.reviewer):
            self.assertNotIn(session.secret, repr(sessions) + repr(audit) + repr(record) + repr(request) + repr(session))
        with self.assertRaises(AdmissionDenied):
            self.broker.bootstrap_operator("other")

    def test_roles_and_forged_approval_are_denied_without_charge(self):
        record, request = self.request()
        before = self.broker.usage(self.host, record["run_id"])
        p = self.proposal(record, "SIMULATE_COMMENT", {"request_id": request["request_id"]})
        for session, proposal in ((self.worker, p), (self.operator, p), (self.host, {**p, "approval": {"decision": "APPROVE"}}),
                                  (self.host, {**p, "action": "COMMENT"})):
            with self.subTest(proposal=proposal), self.assertRaises(AdmissionDenied):
                self.broker.execute(session, proposal)
        with self.assertRaises(AdmissionDenied):
            self.approve(record, request, self.worker)
        self.assertEqual(before, self.broker.usage(self.host, record["run_id"]))

    def test_session_revocation_expiry_and_scope(self):
        record = self.start_case("snapshot")
        token = self.store.acquire(record["run_id"])
        record = self.store.load(record["run_id"])
        p = self.proposal(record, "MOCK_REVIEW", session=self.worker)
        other = PostgresBroker(PostgresRunStore(self.dsn, "another-tenant", "demo-repository", schema=self.schema))
        other_operator = other.bootstrap_operator("operator")
        other_worker = other.grant_session(other_operator, "worker", ["WORKER"])
        with self.assertRaises(AdmissionDenied):
            self.broker.execute(other_worker, p, lease=token)
        with self.assertRaises(ContractError):
            other.execute(other_worker, {**p, "policy_digest": other.context(other_worker)["policy_digest"]}, lease=token)
        self.expire_session(self.worker)
        with self.assertRaises(AdmissionDenied):
            self.broker.execute(self.worker, p, lease=token)
        self.broker.revoke_session(self.operator, self.host)
        with self.assertRaises(AdmissionDenied):
            self.broker.context(self.host)
        with self.assertRaises(AdmissionDenied):
            self.broker.context(LocalSession("x" * 43))
        self.assertEqual([r["charged"] for r in self.store.budget_usage(record["run_id"])], [0, 0, 0])

    def test_snapshot_only_reads_and_live_lease(self):
        record = self.start_case("snapshot")
        token = self.store.acquire(record["run_id"])
        record = self.store.load(record["run_id"])
        path = record["snapshot"]["artifacts"][0]["path"]
        p = self.proposal(record, "READ_SOURCE", {"path": path}, session=self.worker)
        with self.assertRaises(LeaseError):
            self.broker.execute(self.worker, p)
        artifact = self.broker.execute(self.worker, p, lease=token)
        self.assertEqual(artifact, record["snapshot"]["artifacts"][0])
        self.assertEqual(artifact["trust"], "UNTRUSTED_SOURCE")
        for path in ("../connection.json", "not-in-snapshot.py"):
            with self.assertRaises(ContractError):
                self.broker.execute(self.worker, {**p, "arguments": {"path": path}}, lease=token)
        self.store.release(token)
        p = self.proposal(self.store.load(record["run_id"]), "READ_SOURCE", {"path": artifact["path"]}, session=self.worker)
        with self.assertRaises(LeaseError):
            self.broker.execute(self.worker, p, lease=token)
        self.assertEqual(self.broker.usage(self.host, record["run_id"]), 1)

    def test_stale_binding_state_policy_and_revision(self):
        record = self.start_case("snapshot")
        token = self.store.acquire(record["run_id"])
        record = self.store.load(record["run_id"])
        p = self.proposal(record, "MOCK_REVIEW", session=self.worker)
        for key, value in (("expected_version", record["state_version"] - 1), ("binding_digest", digest("forged")), ("policy_digest", digest("forged"))):
            with self.subTest(key=key), self.assertRaises(AdmissionDenied):
                self.broker.execute(self.worker, {**p, key: value}, lease=token)
        self.broker.observe_head(self.operator, record["run_id"], 1, "new-head")
        with self.assertRaises(AdmissionDenied):
            self.broker.execute(self.worker, p, lease=token)
        self.assertEqual(self.broker.usage(self.host, record["run_id"]), 0)

    def test_model_admission_late_session_expiry_rolls_back_every_ledger(self):
        record = self.start_case("snapshot")
        token = self.store.acquire(record["run_id"])
        record = self.store.load(record["run_id"])
        events, wakeups = self.store.events(record["run_id"]), self.store.dispatch_messages(record["run_id"])
        original = self.broker._charge
        def expire_after_charge(db, run_id):
            original(db, run_id)
            db.execute("UPDATE broker_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE token_hash=%s", (self.broker._hash(self.worker),))
        p = self.proposal(record, "MOCK_REVIEW", session=self.worker)
        with patch.object(self.broker, "_charge", expire_after_charge), self.assertRaises(AdmissionDenied):
            self.broker.execute(self.worker, p, lease=token)
        self.assertEqual(self.store.load(record["run_id"]), record)
        self.assertEqual(self.store.events(record["run_id"]), events)
        self.assertEqual(self.store.dispatch_messages(record["run_id"]), wakeups)
        self.assertEqual(self.store.reservations(record["run_id"]), [])
        self.assertEqual([r["charged"] for r in self.store.budget_usage(record["run_id"])], [0, 0, 0])
        self.assertEqual(self.broker.usage(self.host, record["run_id"]), 0)

    def test_model_admission_late_lease_expiry_rolls_back(self):
        record = self.start_case("snapshot")
        token = self.store.acquire(record["run_id"])
        record = self.store.load(record["run_id"])
        original = self.broker._charge
        def expire_after_charge(db, run_id):
            original(db, run_id)
            db.execute("UPDATE runs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s", (run_id,))
        with patch.object(self.broker, "_charge", expire_after_charge), self.assertRaises(LeaseError):
            self.broker.execute(self.worker, self.proposal(record, "MOCK_REVIEW", session=self.worker), lease=token)
        self.assertEqual(self.store.load(record["run_id"]), record)
        self.assertEqual(self.store.reservations(record["run_id"]), [])

    def test_tool_limit_survives_relogin_and_policy_changes(self):
        record = self.start_case("snapshot")
        token = self.store.acquire(record["run_id"])
        record = self.store.load(record["run_id"])
        with self.store.connect() as db:
            db.execute("INSERT INTO broker_usage VALUES(%s,64)", (record["run_id"],))
        fresh = self.broker.grant_session(self.operator, "worker", ["WORKER"])
        self.broker.set_enabled(self.operator, False)
        self.broker.set_enabled(self.operator, True)
        with self.assertRaises(AdmissionDenied):
            self.broker.execute(fresh, self.proposal(record, "MOCK_REVIEW", session=fresh), lease=token)
        self.assertEqual(self.broker.usage(self.host, record["run_id"]), 64)
        self.assertEqual(self.store.load(record["run_id"]), record)
        self.assertEqual([r["charged"] for r in self.store.budget_usage(record["run_id"])], [0, 0, 0])

    def test_separate_reviewer_rejection_and_final_decision(self):
        host_reviewer = self.broker.grant_session(self.operator, "host", ["HOST", "REVIEWER"])
        record, request = self.request()
        with self.assertRaises(AdmissionDenied):
            self.approve(record, request, host_reviewer)
        rejection = self.approve(record, request, decision="REJECT")
        self.assertEqual(self.approve(record, request, decision="REJECT"), rejection)
        with self.assertRaises(AdmissionDenied):
            self.approve(record, request)
        with self.assertRaises(AdmissionDenied):
            self.simulate(record, request)

    def test_payload_run_and_policy_mismatch_cannot_mint_decision(self):
        record, request = self.request()
        for payload, policy in ((digest("changed"), request["policy_digest"]), (request["payload_digest"], digest("changed"))):
            with self.assertRaises(AdmissionDenied):
                self.broker.decide(self.reviewer, record["run_id"], request["request_id"], payload, policy, "APPROVE")
        other = self.start_case()
        with self.assertRaises(AdmissionDenied):
            self.broker.decide(self.reviewer, other["run_id"], request["request_id"], request["payload_digest"], request["policy_digest"], "APPROVE")
        self.assertEqual(self.broker.inspect_request(self.host, record["run_id"], request["request_id"])["status"], "PENDING_HUMAN")

    def test_policy_pause_and_restore_never_revive_approval(self):
        record, request = self.request()
        self.approve(record, request)
        self.broker.set_enabled(self.operator, False)
        with self.assertRaises(AdmissionDenied):
            self.simulate(record, request)
        self.broker.set_enabled(self.operator, True)
        with self.assertRaises(AdmissionDenied):
            self.simulate(record, request)
        self.assertEqual(self.broker.usage(self.host, record["run_id"]), 2)

    def test_revision_change_and_restore_never_revive_approval(self):
        record, request = self.request()
        self.approve(record, request)
        epoch = self.broker.observe_head(self.operator, record["run_id"], 1, "changed")
        with self.assertRaises(AdmissionDenied):
            self.simulate(record, request)
        self.broker.observe_head(self.operator, record["run_id"], epoch, record["binding"]["head_revision"])
        with self.assertRaises(AdmissionDenied):
            self.simulate(record, request)

    def test_issuer_session_and_role_revocation(self):
        record, request = self.request()
        self.approve(record, request)
        self.broker.grant_session(self.operator, "reviewer", ["HOST"])
        with self.assertRaises(AdmissionDenied):
            self.simulate(record, request)
        self.broker.grant_session(self.operator, "reviewer", ["REVIEWER"])
        with self.assertRaises(AdmissionDenied):
            self.simulate(record, request)
        record2, request2 = self.request()
        reviewer2 = self.broker.grant_session(self.operator, "reviewer-2", ["REVIEWER"])
        self.approve(record2, request2, reviewer2)
        self.broker.revoke_session(self.operator, reviewer2)
        with self.assertRaises(AdmissionDenied):
            self.simulate(record2, request2)

    def test_approval_and_pending_expiry_and_explicit_revocation(self):
        record, request = self.request()
        with self.store.connect() as db:
            db.execute("UPDATE broker_requests SET expires_at=clock_timestamp()-interval '1 second' WHERE request_id=%s", (request["request_id"],))
        with self.assertRaises(AdmissionDenied):
            self.approve(record, request)
        record2, request2 = self.request()
        self.approve(record2, request2)
        with self.store.connect() as db:
            db.execute("UPDATE broker_requests SET decision=jsonb_set(decision,'{expires_at}',to_jsonb((clock_timestamp()-interval '1 second')::text)) WHERE request_id=%s", (request2["request_id"],))
        with self.assertRaises(AdmissionDenied):
            self.simulate(record2, request2)
        record3, request3 = self.request()
        self.approve(record3, request3)
        self.broker.revoke_approval(self.reviewer, record3["run_id"], request3["request_id"])
        with self.assertRaises(AdmissionDenied):
            self.simulate(record3, request3)

    def test_duplicate_requests_and_concurrent_simulation_conserve_allowance(self):
        record, request = self.request()
        _, duplicate = self.request(record)
        self.assertEqual(request, duplicate)
        approval = self.approve(record, request)
        self.assertEqual(self.approve(record, request), approval)
        barrier = threading.Barrier(2)
        def simulate():
            barrier.wait()
            return self.simulate(record, request)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(simulate) for _ in range(2)]
            receipts = [f.result(timeout=20) for f in futures]
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.broker.usage(self.host, record["run_id"]), 3)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM broker_audit WHERE kind='SIMULATE_COMMENT'").fetchone()["n"], 1)

    def test_corrupt_preview_rejected_and_receipt_historical_read(self):
        record, request = self.request()
        self.approve(record, request)
        receipt = self.simulate(record, request)
        self.broker.revoke_session(self.operator, self.reviewer)
        self.assertEqual(self.broker.inspect_request(self.host, record["run_id"], request["request_id"])["receipt"], receipt)
        with self.assertRaises(AdmissionDenied):
            self.simulate(record, request)
        with self.store.connect() as db:
            db.execute("UPDATE broker_requests SET preview=jsonb_set(preview,'{body}','\"forged\"') WHERE request_id=%s", (request["request_id"],))
        with self.assertRaises(AdmissionDenied):
            self.broker.inspect_request(self.host, record["run_id"], request["request_id"])

    def test_permission_change_while_action_waits_for_lock_is_rechecked(self):
        record, request = self.request()
        self.approve(record, request)
        # The policy lock is the serialization boundary. An admitted proposal
        # that waits behind a committed revocation must see that revocation.
        started = threading.Event()
        p = self.proposal(record, "SIMULATE_COMMENT", {"request_id": request["request_id"]})
        def simulate():
            started.set()
            return self.broker.execute(self.host, p)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.connect() as db:
                db.execute("SELECT 1 FROM broker_policies WHERE tenant_id=%s AND resource_id=%s FOR UPDATE", self.broker.scope)
                future = pool.submit(simulate)
                self.assertTrue(started.wait(2))
                db.execute("UPDATE broker_policies SET enabled=false,version=version+1 WHERE tenant_id=%s AND resource_id=%s", self.broker.scope)
            with self.assertRaises(AdmissionDenied):
                future.result(timeout=20)
        self.assertEqual(self.broker.usage(self.host, record["run_id"]), 2)
        self.assertIsNone(self.broker.inspect_request(self.host, record["run_id"], request["request_id"])["receipt"])

    def test_python_js_ts_injection_and_partial_coverage_remain_draft_only(self):
        for name, count, outcome in (("JS001", 1, "COMPLETE"), ("TS001", 1, "COMPLETE"),
                                     ("ADV001", 0, "COMPLETE"), ("COV001", 0, "PARTIAL")):
            with self.subTest(case=name):
                fixture = load_case(name)
                controller = self.controller()
                run_id = controller.start(fixture)
                record = controller.advance(run_id, fixture)
                self.assertEqual((len(record["draft"]["findings"]), record["outcome"]), (count, outcome))
                self.assertEqual(record["draft"]["mode"], "DRAFT")
                self.assertEqual(self.broker.usage(self.host, run_id), 1)
                with self.store.connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) AS n FROM broker_requests WHERE run_id=%s", (run_id,)).fetchone()["n"], 0)

    def test_additive_schemas_and_migration_checksums(self):
        record, request = self.request()
        approval = self.approve(record, request)
        receipt = self.simulate(record, request)
        for name, value in (("CommentPreview", request["preview"]), ("SimulationApproval", approval),
                            ("SimulationReceipt", receipt), ("ActionProposal", self.proposal(record, "SIMULATE_COMMENT", {"request_id": request["request_id"]}))):
            validate_record(name, value)
            with self.assertRaises(ContractError):
                validate_record(name, {**value, "unexpected": True})
        self.store.migrate()
        with self.store.connect() as db:
            db.execute("UPDATE schema_migrations SET checksum='changed' WHERE version='003'")
        with self.assertRaises(ContractError):
            self.store.migrate()
