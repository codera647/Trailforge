"""Real-database dispatch authority, outages, duplicates and upgrade cases."""
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parent

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from support import fixture as load_case, fixture_path, register_schema
from trailforge.adapters.mock import FixtureModel
from trailforge.adapters.postgres import PostgresRunStore
from trailforge.controller import OfflineController
from trailforge.core import ContractError, digest
from trailforge.contracts import LeaseError
from trailforge.dispatch import Dispatcher
from trailforge.evidence import capture


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["TRAILFORGE_DATABASE_URL"]
        self.schema = "tf_test_" + uuid4().hex
        self.store = self.new_store()
        register_schema(self.schema)
        self.store.migrate()
        self.addCleanup(self.cleanup)
        self.input = load_case("PY001")
        self.controller = OfflineController(self.store, FixtureModel())

    def new_store(self, tenant="local-demo", resource="demo-repository", **kw):
        return PostgresRunStore(self.dsn, tenant, resource, schema=self.schema, **kw)

    def cleanup(self):
        assert self.schema.startswith("tf_test_") and len(self.schema) == 40
        with psycopg.connect(self.dsn, connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=5000") as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def expire(self, token):
        with self.store.connect() as db:
            db.execute("UPDATE dispatch_outbox SET claim_until=clock_timestamp()-interval '1 second' WHERE message_id=%s", (token.message_id,))

    def ready(self, run_id):
        with self.store.connect() as db:
            db.execute("UPDATE dispatch_outbox SET available_at=clock_timestamp()-interval '1 second' WHERE run_id=%s", (run_id,))

    def test_admission_rollback_has_no_run_event_task_budget_or_intent(self):
        original = self.store._enqueue_dispatch
        def fail(*args):
            original(*args)
            raise RuntimeError("injected transaction failure")
        self.store._enqueue_dispatch = fail
        with self.assertRaises(RuntimeError):
            self.controller.start(self.input)
        with self.store.connect() as db:
            for table in ("runs", "events", "tasks", "budgets", "dispatch_outbox"):
                self.assertEqual(db.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))).fetchone()["n"], 0)

    def test_progress_intent_rollback_restores_checkpoint_and_journal(self):
        run_id = self.controller.start(self.input)
        token = self.store.acquire(run_id)
        before = self.store.load(run_id)
        candidate = {**before, "state": "RUNNING", "stage": "SNAPSHOT", "snapshot": capture(run_id, before["binding"], self.input.sources)}
        original = self.store._enqueue_dispatch
        def fail(*args):
            original(*args)
            raise RuntimeError("injected transaction failure")
        self.store._enqueue_dispatch = fail
        with self.assertRaises(RuntimeError):
            self.store.save(candidate, before["state_version"], "SNAPSHOT_CAPTURED", token=token)
        self.assertEqual(self.store.load(run_id), before)
        self.assertEqual(len(self.store.dispatch_messages(run_id)), 1)
        self.assertEqual(self.store.events(run_id)[-1]["sequence"], before["state_version"])

    def test_duplicate_wakeup_resumes_from_authority_without_extra_model_charge(self):
        run_id = self.controller.start(self.input)
        first, message = self.store.claim_dispatch()
        draft = self.controller.advance(message["run_id"], self.input)["draft"]
        self.expire(first)  # Queue accepted; dispatcher acknowledgement lost.
        second, replay = self.store.claim_dispatch()
        self.assertEqual(message, replay)
        self.assertGreater(second.epoch, first.epoch)
        self.assertEqual(self.controller.advance(replay["run_id"], self.input)["draft"], draft)
        self.store.acknowledge_dispatch(second)
        self.assertEqual(self.store.load(run_id)["model_calls"], 1)
        self.assertEqual(len(self.store.reservations(run_id)), 1)
        self.assertTrue(all(u["charged"] == 1 for u in self.store.budget_usage(run_id)))

    def test_concurrent_claim_has_one_owner(self):
        self.controller.start(self.input)
        stores = [self.new_store(), self.new_store()]
        barrier = threading.Barrier(2)
        def claim(store):
            barrier.wait()
            return store.claim_dispatch()
        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(claim, stores))
        self.assertEqual(sum(c is not None for c in claims), 1)

    def test_replaced_epoch_rejects_old_ack_and_retry(self):
        run_id = self.controller.start(self.input)
        old, _ = self.store.claim_dispatch()
        self.expire(old)
        other = self.new_store()
        new, _ = other.claim_dispatch()
        for action in (self.store.acknowledge_dispatch, self.store.retry_dispatch):
            with self.assertRaises(LeaseError):
                action(old)
        other.acknowledge_dispatch(new)
        self.assertEqual(self.store.dispatch_messages(run_id)[0]["status"], "DELIVERED")

    def test_expiry_at_ack_mutation_is_rechecked(self):
        run_id = self.controller.start(self.input)
        token, _ = self.store.claim_dispatch()
        original = self.store._finish_dispatch
        def expire_after_read(db, *args, **kwargs):
            db.execute("UPDATE dispatch_outbox SET claim_until=clock_timestamp()-interval '1 second' WHERE message_id=%s", (token.message_id,))
            return original(db, *args, **kwargs)
        self.store._finish_dispatch = expire_after_read
        with self.assertRaises(LeaseError):
            self.store.acknowledge_dispatch(token)
        self.assertEqual(self.store.dispatch_messages(run_id)[0]["status"], "CLAIMED")

    def test_transport_outage_backoff_is_durable_and_errors_are_redacted(self):
        run_id = self.controller.start(self.input)
        class Down:
            def send(self, message):
                raise RuntimeError("password=must-not-be-stored")
        with self.assertRaises(RuntimeError) as failure:
            Dispatcher(self.store, Down()).dispatch_one()
        self.assertNotIn("must-not-be-stored", str(failure.exception))
        self.assertIsNone(self.new_store().claim_dispatch())
        messages = self.store.dispatch_messages(run_id)
        self.assertEqual(messages[0]["status"], "PENDING")
        self.assertEqual(messages[0]["error_code"], "TRANSPORT_FAILURE")
        self.assertNotIn("must-not-be-stored", json.dumps(messages))
        self.ready(run_id)
        accepted = []
        class Up:
            def send(self, message):
                accepted.append(message)
        self.assertTrue(Dispatcher(self.new_store(), Up()).dispatch_one())
        self.assertEqual(len(accepted), 1)
        self.assertEqual(self.store.dispatch_messages(run_id)[0]["attempts"], 2)

    def test_five_failures_exhaust_without_losing_accepted_run(self):
        run_id = self.controller.start(self.input)
        for attempt in range(5):
            token, _ = self.new_store().claim_dispatch()
            self.store.retry_dispatch(token)
            self.ready(run_id)
        self.assertIsNone(self.store.claim_dispatch())
        message = self.store.dispatch_messages(run_id)[0]
        self.assertEqual((message["status"], message["attempts"]), ("EXHAUSTED", 5))
        self.assertEqual(self.store.load(run_id)["stage"], "ADMITTED")
        self.assertEqual(self.store.load(run_id)["model_calls"], 0)

    def test_five_crashes_exhaust_and_do_not_hide_next_ready_run(self):
        run_id = self.controller.start(self.input)
        for attempt in range(5):
            token, _ = self.store.claim_dispatch()
            self.expire(token)
        next_run = self.controller.start(self.input)
        _, next_message = self.store.claim_dispatch()
        self.assertEqual(next_message["run_id"], next_run)
        self.assertEqual(self.store.dispatch_messages(run_id)[0]["status"], "EXHAUSTED")

    def test_cross_tenant_and_resource_cannot_claim_read_or_ack(self):
        run_id = self.controller.start(self.input)
        token, _ = self.store.claim_dispatch()
        for outsider in (self.new_store(tenant="other"), self.new_store(resource="other")):
            self.assertIsNone(outsider.claim_dispatch())
            with self.assertRaises(ContractError):
                outsider.dispatch_messages(run_id)
            with self.assertRaises(ContractError):
                outsider.acknowledge_dispatch(token)

    def test_forged_binding_or_effect_message_cannot_dispatch(self):
        run_id = self.controller.start(self.input)
        original = self.store.dispatch_messages(run_id)[0]["message"]
        for change in ({"binding_digest": "sha256:" + "0" * 64}, {"kind": "COMMENT"}, {"allow_publish": True}):
            forged = {**original, **change}
            with self.store.connect() as db:
                db.execute("UPDATE dispatch_outbox SET payload=%s,checksum=%s WHERE run_id=%s", (Jsonb(forged), digest(forged), run_id))
            with self.assertRaises(ContractError):
                self.store.claim_dispatch()

    def test_upgrade_backfills_nonterminal_runs_once(self):
        pending = self.controller.start(self.input)
        terminal = self.controller.start(self.input)
        self.controller.advance(terminal, self.input)
        with self.store.connect() as db:
            db.execute("DROP TABLE dispatch_outbox")
            db.execute("DELETE FROM schema_migrations WHERE version='002'")
        self.store.migrate()
        self.store.migrate()
        self.assertEqual(len(self.store.dispatch_messages(pending)), 1)
        self.assertEqual(self.store.dispatch_messages(terminal), [])
        self.assertEqual(self.store.load(terminal)["stage"], "DRAFTED")

    def test_dispatch_migration_drift_is_rejected(self):
        with self.store.connect() as db:
            db.execute("UPDATE schema_migrations SET checksum='changed' WHERE version='002'")
        with self.assertRaises(ContractError):
            self.store.migrate()

    def test_future_database_migration_cannot_be_silently_downgraded(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO schema_migrations VALUES ('999', 'future')")
        with self.assertRaises(ContractError):
            self.store.migrate()

    def test_real_process_crash_after_acceptance_replays_stable_identity(self):
        run_id = self.controller.start(self.input)
        with tempfile.TemporaryDirectory() as folder:
            receipt = Path(folder) / "accepted.json"
            result = subprocess.run([sys.executable, "-I", str(ROOT / "dispatch_crash_worker.py"),
                                     self.schema, str(receipt)], cwd=ROOT, timeout=30)
            self.assertEqual(result.returncode, 23)
            accepted = json.loads(receipt.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 10
            claimed = self.store.claim_dispatch()
            while claimed is None and time.monotonic() < deadline:
                time.sleep(0.05)
                claimed = self.store.claim_dispatch()
            self.assertIsNotNone(claimed)
            token, replay = claimed
            self.assertEqual(replay, accepted)
            self.store.acknowledge_dispatch(token)
            self.assertEqual(self.store.dispatch_messages(run_id)[0]["attempts"], 2)

    def test_database_preflight_failure_replaces_stale_pass_and_redacts_connection(self):
        import socket
        import shutil
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            unused_port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as folder:
            isolated = Path(folder)
            (isolated / "scripts").mkdir()
            runner = isolated / "scripts/run_suite.py"
            shutil.copy2(ROOT / "run_suite.py", runner)
            report = isolated / ".local/verification/foundation/report.json"
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
            env = os.environ.copy()
            env["TRAILFORGE_DATABASE_URL"] = psycopg.conninfo.make_conninfo(host="127.0.0.1", port=unused_port,
                user="negative-control", password="not-for-output", dbname="postgres")
            result = subprocess.run([sys.executable, "-I", str(runner), "--receipt", str(report)], cwd=isolated, env=env,
                capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("not-for-output", result.stdout + result.stderr)
            actual = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual((actual["status"], actual["stage"]), ("FAIL", "DATABASE_PREFLIGHT"))
            self.assertTrue((report.parent / "attempts/1-report.json").exists())
