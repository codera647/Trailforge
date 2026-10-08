"""Real database lease, budget, atomicity and process-recovery regressions."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parent

import psycopg
from psycopg import sql

from support import fixture as load_case, fixture_path, register_schema
from trailforge.adapters.mock import FixtureModel
from trailforge.adapters.postgres import PostgresRunStore
from trailforge.adapters.schema import validate_record
from trailforge.contracts import BudgetError, LeaseError, make_draft
from trailforge.controller import OfflineController
from trailforge.core import ContractError, ConflictError, digest
from trailforge.evidence import capture


class PostgresTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("TRAILFORGE_DATABASE_URL"):
            raise RuntimeError("Real PostgreSQL required; run scripts/verify_postgres.py")
        self.dsn = os.environ["TRAILFORGE_DATABASE_URL"]
        self.schema = "tf_test_" + uuid4().hex
        self.store = self.new_store()
        register_schema(self.schema)
        self.store.migrate()
        self.addCleanup(self.cleanup_schema)
        self.model = FixtureModel()
        self.controller = OfflineController(self.store, self.model)

    def new_store(self, tenant="local-demo", resource="demo-repository", **kwargs):
        return PostgresRunStore(self.dsn, tenant, resource, schema=self.schema, **kwargs)

    def cleanup_schema(self):
        # Only the uniquely generated schema this test created is removed.
        assert self.schema.startswith("tf_test_") and len(self.schema) == 40
        with psycopg.connect(self.dsn, connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=5000") as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def fixture(self, name="PY001"):
        return load_case(name)

    def run_case(self, name="PY001", pause=None, store=None):
        store = store or self.store
        controller = OfflineController(store, self.model)
        review_input = self.fixture(name)
        run_id = controller.start(review_input)
        return controller.advance(run_id, review_input, pause), review_input

    def ready_for_admission(self, store=None):
        store = store or self.store
        record, review_input = self.run_case(pause="snapshot", store=store)
        token = store.acquire(record["run_id"])
        return store.load(record["run_id"]), token, review_input

    def test_all_original_fixtures_against_real_store(self):
        expected = {"PY001": (1, "COMPLETE"), "PY002": (0, "COMPLETE"), "JS001": (1, "COMPLETE"),
                    "JS002": (0, "COMPLETE"), "TS001": (1, "COMPLETE"), "TS002": (0, "COMPLETE"),
                    "ADV001": (0, "COMPLETE"), "COV001": (0, "PARTIAL"), "COV002": (0, "PARTIAL"), "COV003": (0, "PARTIAL")}
        for name, (count, outcome) in expected.items():
            with self.subTest(case=name):
                record, _ = self.run_case(name)
                validate_record("RunCheckpoint", record)
                self.assertEqual((len(record["draft"]["findings"]), record["outcome"]), (count, outcome))
                self.assertEqual(len(self.store.tasks(record["run_id"])), 4)
                self.assertEqual(self.store.reservations(record["run_id"])[0]["state"], "SETTLED")

    def test_resume_retains_journal_and_draft_is_idempotent(self):
        for pause in ("snapshot", "review", "validation"):
            with self.subTest(pause=pause):
                record, review_input = self.run_case(pause=pause)
                fresh = OfflineController(self.new_store(), self.model)
                done = fresh.advance(record["run_id"], review_input)
                events = self.store.events(record["run_id"])
                self.assertEqual(done, fresh.advance(record["run_id"], review_input))
                self.assertEqual(events, self.store.events(record["run_id"]))
                self.assertEqual(done["model_calls"], 1)
                self.assertEqual([e["sequence"] for e in events], list(range(len(events))))

    def test_two_workers_only_one_acquires_live_lease(self):
        record, _ = self.run_case(pause="snapshot")
        barrier = threading.Barrier(2)
        def acquire():
            store = self.new_store()
            barrier.wait()
            try:
                return store, store.acquire(record["run_id"])
            except LeaseError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: acquire(), range(2)))
        wins = [r for r in results if r]
        self.assertEqual(len(wins), 1)
        wins[0][0].release(wins[0][1])

    def test_expired_epoch_cannot_commit_or_reserve(self):
        short = self.new_store()
        record, token, _ = self.ready_for_admission(short)
        # Set expiry after setup, using the same authoritative database clock.
        # A tiny setup TTL would test connection speed instead of fencing.
        with short.connect() as db:
            db.execute("UPDATE runs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s", (record["run_id"],))
        replacement = self.new_store()
        new_token = replacement.acquire(record["run_id"])
        self.assertGreater(new_token.epoch, token.epoch)
        with self.assertRaises(LeaseError):
            short.admit_model(record, token)
        candidate = {**record, "state_version": replacement.load(record["run_id"])["state_version"]}
        with self.assertRaises(LeaseError):
            short.save(candidate, candidate["state_version"], "MOCK_REVIEW_RECORDED", token=token)
        with self.assertRaises(LeaseError):
            short.release(token)
        replacement.release(new_token)

    def test_stale_version_cannot_duplicate_admission(self):
        record, token, _ = self.ready_for_admission()
        admitted = self.store.admit_model(record, token)
        with self.assertRaises(ConflictError):
            self.store.admit_model(record, token)
        self.assertEqual(admitted["model_calls"], 1)
        self.assertEqual(len(self.store.reservations(record["run_id"])), 1)
        self.store.release(token)

    def test_stale_version_cannot_commit_a_second_checkpoint(self):
        review_input = self.fixture()
        run_id = self.controller.start(review_input)
        token = self.store.acquire(run_id)
        previous = self.store.load(run_id)
        candidate = {**previous, "stage": "SNAPSHOT", "state": "RUNNING",
                     "snapshot": capture(run_id, previous["binding"], review_input.sources)}
        updated = self.store.save(candidate, previous["state_version"], "SNAPSHOT_CAPTURED", token=token)
        with self.assertRaises(ConflictError):
            self.store.save(candidate, previous["state_version"], "SNAPSHOT_CAPTURED", token=token)
        self.assertEqual(self.store.load(run_id), updated)
        self.store.release(token)

    def test_source_and_coverage_claims_cannot_override_store_gates(self):
        record, _ = self.run_case(pause="snapshot")
        corrupted = deepcopy(record)
        corrupted["snapshot"]["artifacts"][0]["content"] = "invented"
        with self.assertRaises(ContractError):
            self.store.save(corrupted, corrupted["state_version"], "MOCK_REVIEW_RECORDED")
        partial, _ = self.run_case("COV001", pause="validation")
        forged = deepcopy(partial)
        forged["validation"]["coverage"]["gaps"] = []
        draft = make_draft(forged)
        forged.update(draft=draft, stage="DRAFTED", state="TERMINAL", outcome="COMPLETE")
        with self.assertRaisesRegex(ContractError, "recorded validation"):
            self.store.save(forged, forged["state_version"], "DRAFT_SAVED")

    def test_last_tenant_unit_is_reserved_by_only_one_run(self):
        self.last_unit_race(tenant_limit=1, global_limit=1000)

    def test_last_global_unit_is_reserved_by_only_one_run(self):
        self.last_unit_race(tenant_limit=100, global_limit=1)

    def last_unit_race(self, tenant_limit, global_limit):
        # A fresh isolated schema gives this test authoritative one-unit ledgers.
        stores = [self.new_store(tenant_limit=tenant_limit, global_limit=global_limit) for _ in range(2)]
        ready = [self.ready_for_admission(store) for store in stores]
        barrier = threading.Barrier(2)
        def reserve(index):
            record, token, _ = ready[index]
            barrier.wait()
            try:
                return stores[index].admit_model(record, token)
            except BudgetError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(sum(len(self.store.reservations(r[0]["run_id"])) for r in ready), 1)
        for store, (record, token, _) in zip(stores, ready):
            usage = store.budget_usage(record["run_id"])
            limiting = "TENANT" if tenant_limit == 1 else "GLOBAL"
            self.assertEqual(next(u["charged"] for u in usage if u["scope_kind"] == limiting), 1)
            store.release(token)

    def test_missing_lease_cannot_mutate_or_admit(self):
        record, _ = self.run_case(pause="snapshot")
        with self.assertRaises(LeaseError):
            self.store.save(record, record["state_version"], "EVIDENCE_VALIDATED")
        with self.assertRaises(LeaseError):
            self.store.admit_model(record, None)

    def test_cross_tenant_resource_state_events_and_artifacts_denied(self):
        record, _ = self.run_case()
        for store in (self.new_store(tenant="other"), self.new_store(resource="other")):
            for operation in (store.load, store.events, store.tasks, store.reservations, store.budget_usage, store.acquire):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaises(ContractError):
                        operation(record["run_id"])

    def test_admission_and_journal_rollback_together(self):
        record, token, _ = self.ready_for_admission()
        before = self.store.budget_usage(record["run_id"])
        original_commit = self.store._commit
        def fail(*args, **kwargs):
            raise RuntimeError("injected fault after reservations, before journal")
        self.store._commit = fail
        try:
            with self.assertRaises(RuntimeError):
                self.store.admit_model(record, token)
        finally:
            self.store._commit = original_commit
        self.assertEqual(self.store.load(record["run_id"]), record)
        self.assertEqual(self.store.budget_usage(record["run_id"]), before)
        self.assertEqual(self.store.reservations(record["run_id"]), [])
        self.store.release(token)

    def test_crashed_mock_charges_all_scopes_and_recovers(self):
        class Crash(FixtureModel):
            def review(self, snapshot, configured_output):
                raise RuntimeError("injected crash")
        review_input = self.fixture()
        run_id = self.controller.start(review_input)
        with self.assertRaises(RuntimeError):
            OfflineController(self.store, Crash()).advance(run_id, review_input)
        self.assertEqual(self.store.load(run_id)["model_calls"], 1)
        self.assertTrue(all(u["charged"] == 1 for u in self.store.budget_usage(run_id)))
        self.assertEqual(self.store.reservations(run_id)[0]["state"], "CHARGED")
        done = OfflineController(self.new_store(), self.model).advance(run_id, review_input)
        self.assertEqual(done["model_calls"], 2)
        self.assertEqual([r["state"] for r in self.store.reservations(run_id)], ["CHARGED", "SETTLED"])

    def test_repeated_crashes_never_reset_run_budget(self):
        class Crash(FixtureModel):
            def review(self, snapshot, configured_output):
                raise RuntimeError("injected crash")
        review_input = self.fixture()
        run_id = self.controller.start(review_input)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                OfflineController(self.new_store(), Crash()).advance(run_id, review_input)
        with self.assertRaises(ContractError):
            self.controller.advance(run_id, review_input)
        self.assertEqual(self.store.load(run_id)["model_calls"], 2)
        self.assertEqual(len(self.store.reservations(run_id)), 2)

    def test_hard_worker_crash_retains_charge_and_recovers_after_expiry(self):
        record, review_input = self.run_case(pause="snapshot")
        process = subprocess.run([sys.executable, "-I", str(ROOT / "crash_worker.py"),
                                  self.schema, record["run_id"], str(fixture_path("PY001"))], cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 23, process.stderr)
        persisted = self.store.load(record["run_id"])
        self.assertEqual(persisted["model_calls"], 1)
        self.assertEqual(self.store.reservations(record["run_id"])[0]["state"], "CHARGED")
        with self.store.connect() as db:
            remaining = db.execute("SELECT GREATEST(0,extract(epoch FROM lease_until-clock_timestamp())) AS remaining FROM runs WHERE run_id=%s",
                                   (record["run_id"],)).fetchone()["remaining"]
        time.sleep(float(remaining) + 0.1)
        done = self.controller.advance(record["run_id"], review_input)
        self.assertEqual(done["model_calls"], 2)
        self.assertEqual(done["outcome"], "COMPLETE")

    def test_changed_applied_migration_is_rejected(self):
        with self.store.connect() as db:
            db.execute("UPDATE schema_migrations SET checksum='changed' WHERE version='001'")
        with self.assertRaisesRegex(ContractError, "migration checksum"):
            self.store.migrate()

    def test_budget_policy_drift_and_rebinding_are_rejected(self):
        record, review_input = self.run_case(pause="snapshot")
        with self.assertRaises(ContractError):
            OfflineController(self.new_store(tenant_limit=999), self.model).start(review_input)
        changed = replace(review_input, binding=replace(review_input.binding, head_revision="new-head"))
        with self.assertRaises(ContractError):
            self.controller.advance(record["run_id"], changed)

    def test_corrupted_checkpoint_is_not_resumed(self):
        record, review_input = self.run_case(pause="snapshot")
        with self.store.connect() as db:
            db.execute("UPDATE runs SET checksum='corrupted' WHERE run_id=%s", (record["run_id"],))
        with self.assertRaises(ContractError):
            self.controller.advance(record["run_id"], review_input)

    def test_lease_expiring_during_admission_rolls_back_units(self):
        short = self.new_store()
        record, token, _ = self.ready_for_admission(short)
        original = short._commit
        def delayed(*args, **kwargs):
            db = args[0]
            db.execute("UPDATE runs SET lease_until=clock_timestamp()+interval '100 milliseconds' WHERE run_id=%s", (record["run_id"],))
            db.execute("SELECT pg_sleep(0.15)")
            return original(*args, **kwargs)
        short._commit = delayed
        try:
            with self.assertRaises(LeaseError):
                short.admit_model(record, token)
        finally:
            short._commit = original
        self.assertEqual(short.load(record["run_id"])["model_calls"], 0)
        self.assertEqual(short.reservations(record["run_id"]), [])
        self.assertTrue(all(u["charged"] == 0 for u in short.budget_usage(record["run_id"])))
        short.release(token)

    def test_separate_process_controller_pause_and_resume(self):
        args = [sys.executable, "-I", str(ROOT / "controller_worker.py"), self.schema, str(fixture_path("PY001"))]
        first = subprocess.run(args + ["pause"],
                               capture_output=True, text=True, check=True, cwd=ROOT, timeout=20)
        paused = json.loads(first.stdout)
        second = subprocess.run(args + ["resume", paused["run_id"]],
                                capture_output=True, text=True, check=True, cwd=ROOT, timeout=20)
        self.assertEqual(json.loads(second.stdout)["workflow_outcome"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
