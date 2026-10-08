"""Postgres local capability broker; no customer login or external-effect port.

The database administrator and composition root are trusted. Policy-row locking
serializes each local scope. All public action inputs are untrusted proposals.
"""
from contextlib import contextmanager
from hashlib import sha256
import secrets
from uuid import uuid4

from psycopg.types.json import Jsonb

from ..broker import (BROKER_POLICY, ROLES, AdmissionDenied, LocalSession, action_proposal,
                      authorize, comment_preview, policy_digest, validate_proposal)
from ..contracts import PROTOCOL_VERSION, assert_approval_binding, integer
from ..core import ContractError, digest, nonempty
from .schema import validate_record


class PostgresBroker:
    def __init__(self, store, *, external_session_guard=None):
        self.store = store
        self._external_session_guard = external_session_guard

    def _external_authority(self, db, kind, token_hash, actor_id):
        if kind == "LOCAL":
            return
        if kind != "EXTERNAL_IDENTITY" or self._external_session_guard is None:
            raise AdmissionDenied("EXTERNAL_AUTHORITY_REQUIRED")
        try:
            permitted = self._external_session_guard(db, token_hash, actor_id)
        except Exception:
            raise AdmissionDenied("EXTERNAL_AUTHORITY_DENIED") from None
        if permitted is not True:
            raise AdmissionDenied("EXTERNAL_AUTHORITY_DENIED")

    @property
    def scope(self):
        return self.store.tenant_id, self.store.resource_id

    @staticmethod
    def _hash(session):
        if not isinstance(session, LocalSession) or type(session.secret) is not str or len(session.secret) != 43:
            raise AdmissionDenied("SESSION_DENIED")
        return sha256(session.secret.encode("utf-8")).hexdigest()

    def _policy(self, db):
        row = db.execute("SELECT * FROM broker_policies WHERE tenant_id=%s AND resource_id=%s FOR UPDATE", self.scope).fetchone()
        if row is None:
            raise AdmissionDenied("BROKER_NOT_INITIALIZED")
        return row

    def _digest(self, policy):
        return policy_digest(*self.scope, policy["version"], policy["enabled"])

    def _auth(self, db, session, role=None):
        row = db.execute("""SELECT s.token_hash,s.actor_id,s.expires_at,s.authority_kind,p.roles,p.version,
            s.expires_at > clock_timestamp() AND NOT s.revoked AS live
            FROM broker_sessions s JOIN broker_principals p USING(tenant_id,resource_id,actor_id)
            WHERE s.token_hash=%s AND s.tenant_id=%s AND s.resource_id=%s""",
            (self._hash(session), *self.scope)).fetchone()
        if row is None or not row["live"]:
            raise AdmissionDenied("SESSION_DENIED")
        if (type(row["roles"]) is not list or not row["roles"] or
                any(type(r) is not str or r not in ROLES for r in row["roles"])):
            raise AdmissionDenied("ROLE_DENIED")
        if role and role not in row["roles"]:
            raise AdmissionDenied("ROLE_DENIED")
        self._external_authority(db, row["authority_kind"], row["token_hash"], row["actor_id"])
        return row

    @contextmanager
    def _transaction(self, session, role=None, *, allow_paused=False):
        with self.store.connect() as db:
            policy = self._policy(db)
            actor = self._auth(db, session, role)
            if not allow_paused and not policy["enabled"]:
                raise AdmissionDenied("POLICY_PAUSED")
            yield db, policy, actor
            # Database time advances during lock waits and work. Reject expired
            # sessions at the final admission boundary and roll back all writes.
            self._auth(db, session, role)

    def _audit(self, db, actor, kind, run_id=None, **details):
        db.execute("INSERT INTO broker_audit(tenant_id,resource_id,actor_id,run_id,kind,details) VALUES(%s,%s,%s,%s,%s,%s)",
                   (*self.scope, actor["actor_id"], run_id, kind, Jsonb(details)))

    def _mint(self, db, actor_id, seconds):
        session = LocalSession(secrets.token_urlsafe(32))
        db.execute("""INSERT INTO broker_sessions(token_hash,tenant_id,resource_id,actor_id,expires_at)
            VALUES(%s,%s,%s,%s,clock_timestamp()+(%s * interval '1 second'))""",
            (self._hash(session), *self.scope, actor_id, seconds))
        return session

    def bootstrap_operator(self, actor_id):
        """Privileged composition-only API. Once per scope, never a model tool."""
        nonempty(actor_id)
        with self.store.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("broker-bootstrap:" + digest(self.scope),))
            if db.execute("SELECT 1 FROM broker_policies WHERE tenant_id=%s AND resource_id=%s", self.scope).fetchone():
                raise AdmissionDenied("ALREADY_INITIALIZED")
            db.execute("INSERT INTO broker_policies VALUES(%s,%s,1,true)", self.scope)
            db.execute("INSERT INTO broker_principals VALUES(%s,%s,%s,%s,1)", (*self.scope, actor_id, Jsonb(["OPERATOR"])))
            result = self._mint(db, actor_id, BROKER_POLICY["session_seconds"])
            self._audit(db, {"actor_id": actor_id}, "BOOTSTRAPPED")
            return result

    def grant_session(self, operator, actor_id, roles, *, seconds=3600):
        nonempty(actor_id)
        integer(seconds, minimum=1, maximum=BROKER_POLICY["session_seconds"])
        if type(roles) is not list or not roles or any(type(r) is not str or r not in ROLES for r in roles) or len(set(roles)) != len(roles):
            raise AdmissionDenied("INVALID_ROLES")
        with self._transaction(operator, "OPERATOR", allow_paused=True) as (db, _, actor):
            db.execute("""INSERT INTO broker_principals VALUES(%s,%s,%s,%s,1)
                ON CONFLICT(tenant_id,resource_id,actor_id) DO UPDATE
                SET roles=excluded.roles,version=broker_principals.version+1""", (*self.scope, actor_id, Jsonb(roles)))
            result = self._mint(db, actor_id, seconds)
            self._audit(db, actor, "SESSION_GRANTED", target_actor=actor_id, roles=roles)
            return result

    def revoke_session(self, operator, target):
        with self._transaction(operator, "OPERATOR", allow_paused=True) as (db, _, actor):
            changed = db.execute("UPDATE broker_sessions SET revoked=true WHERE token_hash=%s AND tenant_id=%s AND resource_id=%s",
                                 (self._hash(target), *self.scope)).rowcount
            if not changed:
                raise AdmissionDenied("SESSION_DENIED")
            self._audit(db, actor, "SESSION_REVOKED")

    def set_enabled(self, operator, enabled):
        if type(enabled) is not bool:
            raise AdmissionDenied("INVALID_POLICY")
        with self._transaction(operator, "OPERATOR", allow_paused=True) as (db, policy, actor):
            db.execute("UPDATE broker_policies SET enabled=%s,version=version+1 WHERE tenant_id=%s AND resource_id=%s", (enabled, *self.scope))
            self._audit(db, actor, "POLICY_CHANGED", version=policy["version"] + 1, enabled=enabled)

    def context(self, session):
        with self._transaction(session, allow_paused=True) as (_, policy, actor):
            return {"policy_digest": self._digest(policy), "enabled": policy["enabled"],
                    "actor_id": actor["actor_id"], "roles": list(actor["roles"])}

    def _run(self, db, run_id):
        row = self.store._row(db, run_id, lock=True)
        record = self.store._decode(db, row)
        db.execute("INSERT INTO broker_targets VALUES(%s,%s,1) ON CONFLICT DO NOTHING", (run_id, record["binding"]["head_revision"]))
        target = db.execute("SELECT * FROM broker_targets WHERE run_id=%s FOR UPDATE", (run_id,)).fetchone()
        return row, record, target

    def observe_head(self, operator, run_id, expected_epoch, head_revision):
        nonempty(head_revision)
        integer(expected_epoch, minimum=1)
        with self._transaction(operator, "OPERATOR", allow_paused=True) as (db, _, actor):
            _, _, target = self._run(db, run_id)
            if target["epoch"] != expected_epoch:
                raise AdmissionDenied("REVISION_CHANGED")
            db.execute("UPDATE broker_targets SET head_revision=%s,epoch=epoch+1 WHERE run_id=%s", (head_revision, run_id))
            self._audit(db, actor, "LOCAL_HEAD_OBSERVED", run_id, epoch=expected_epoch + 1)
            return expected_epoch + 1

    def _charge(self, db, run_id):
        db.execute("INSERT INTO broker_usage VALUES(%s,0) ON CONFLICT DO NOTHING", (run_id,))
        if db.execute("UPDATE broker_usage SET accepted=accepted+1 WHERE run_id=%s AND accepted < %s", (run_id, BROKER_POLICY["tool_limit"])).rowcount != 1:
            raise AdmissionDenied("TOOL_ALLOWANCE_EXHAUSTED")

    def _request(self, db, request_id, run_id):
        result = db.execute("SELECT *,expires_at > clock_timestamp() AS pending_live FROM broker_requests WHERE request_id=%s AND run_id=%s FOR UPDATE",
                            (request_id, run_id)).fetchone()
        if result is None:
            raise AdmissionDenied("REQUEST_UNAVAILABLE")
        validate_record("CommentPreview", result["preview"])
        if digest(result["preview"]) != result["payload_digest"]:
            raise AdmissionDenied("PAYLOAD_CHANGED")
        return result

    def execute(self, session, proposal, *, lease=None):
        validate_proposal(proposal)
        validate_record("ActionProposal", proposal)
        with self._transaction(session) as (db, policy, actor):
            row, record, target = self._run(db, proposal["run_id"])
            authorize(proposal, record, actor["roles"], self._digest(policy), target["head_revision"])
            action, args = proposal["action"], proposal["arguments"]
            if action in ("READ_SOURCE", "MOCK_REVIEW"):
                self.store._fence(row, lease)
            charge = True
            if action == "READ_SOURCE":
                result = next((a for a in record["snapshot"]["artifacts"] if a["path"] == args["path"]), None)
                if result is None:
                    raise AdmissionDenied("SOURCE_UNAVAILABLE")
            elif action == "MOCK_REVIEW":
                result = self.store._admit_model(db, record, lease)
            elif action == "REQUEST_SIMULATION":
                preview = comment_preview(record)
                validate_record("CommentPreview", preview)
                payload_digest = digest(preview)
                request_id = digest({"run": record["run_id"], "actor": actor["actor_id"],
                                     "policy": self._digest(policy), "epoch": target["epoch"], "payload": payload_digest})
                existing = db.execute("SELECT 1 FROM broker_requests WHERE request_id=%s", (request_id,)).fetchone()
                if existing:
                    request = self._request(db, request_id, record["run_id"])
                    if not request["pending_live"]:
                        raise AdmissionDenied("REQUEST_EXPIRED")
                    charge = False
                else:
                    db.execute("""INSERT INTO broker_requests(request_id,run_id,requester,policy_digest,target_epoch,
                        payload_digest,preview,expires_at,status) VALUES(%s,%s,%s,%s,%s,%s,%s,
                        clock_timestamp()+(%s * interval '1 second'),'PENDING_HUMAN')""",
                        (request_id, record["run_id"], actor["actor_id"], self._digest(policy), target["epoch"],
                         payload_digest, Jsonb(preview), BROKER_POLICY["pending_seconds"]))
                result = self._view(self._request(db, request_id, record["run_id"]))
            else:
                request = self._request(db, args["request_id"], record["run_id"])
                self._match_request(request, record, policy, target)
                if request["requester"] != actor["actor_id"]:
                    raise AdmissionDenied("REQUESTER_DENIED")
                self._valid_approval(db, request, record, policy)
                if request["status"] == "SIMULATED":
                    validate_record("SimulationReceipt", request["receipt"])
                    if request["receipt"] != self._receipt(request):
                        raise AdmissionDenied("PAYLOAD_CHANGED")
                    result, charge = request["receipt"], False
                else:
                    result = self._receipt(request)
                    validate_record("SimulationReceipt", result)
                    db.execute("UPDATE broker_requests SET status='SIMULATED',receipt=%s WHERE request_id=%s", (Jsonb(result), request["request_id"]))
            if charge:
                self._charge(db, record["run_id"])
                self._audit(db, actor, action, record["run_id"], policy_digest=self._digest(policy), result_digest=digest(result))
            # A source read can outlive its initial lease check during work.
            if action in ("READ_SOURCE", "MOCK_REVIEW"):
                self.store._fence(self.store._row(db, record["run_id"]), lease)
            if action == "SIMULATE_COMMENT":
                self._valid_approval(db, request, record, policy)
            return result

    def _match_request(self, request, record, policy, target):
        if request["policy_digest"] != self._digest(policy):
            raise AdmissionDenied("POLICY_CHANGED")
        if request["target_epoch"] != target["epoch"] or target["head_revision"] != record["binding"]["head_revision"]:
            raise AdmissionDenied("REVISION_CHANGED")
        if request["preview"] != comment_preview(record):
            raise AdmissionDenied("PAYLOAD_CHANGED")

    def _valid_approval(self, db, request, record, policy):
        if request["status"] not in ("APPROVED", "SIMULATED") or not request["decision"]:
            raise AdmissionDenied("APPROVAL_REQUIRED")
        issuer = db.execute("""SELECT s.actor_id,s.authority_kind,s.expires_at > clock_timestamp() AND NOT s.revoked AS live,p.roles,p.version
            FROM broker_sessions s JOIN broker_principals p USING(tenant_id,resource_id,actor_id)
            WHERE s.token_hash=%s AND s.tenant_id=%s AND s.resource_id=%s""", (request["issuer_session"], *self.scope)).fetchone()
        if (issuer is None or not issuer["live"] or "REVIEWER" not in issuer["roles"] or
                issuer["version"] != request["issuer_version"] or issuer["actor_id"] != request["decision"]["actor_id"]):
            raise AdmissionDenied("ISSUER_REVOKED")
        self._external_authority(db, issuer["authority_kind"], request["issuer_session"], issuer["actor_id"])
        validate_record("SimulationApproval", request["decision"])
        now = db.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        try:
            assert_approval_binding(request["decision"], digest(record["binding"]), request["payload_digest"],
                                    self._digest(policy), "SIMULATE_COMMENT", now, run_id=record["run_id"])
        except ContractError:
            raise AdmissionDenied("APPROVAL_EXPIRED_OR_CHANGED") from None

    @staticmethod
    def _receipt(request):
        return {"schema_version": PROTOCOL_VERSION, "kind": "LOCAL_COMMENT_SIMULATION",
                "receipt_id": digest({"request": request["request_id"], "approval": request["decision"]["approval_id"]}),
                "request_id": request["request_id"], "run_id": request["run_id"],
                "payload_digest": request["payload_digest"], "policy_digest": request["policy_digest"],
                "approval_id": request["decision"]["approval_id"], "external_effect": "NONE"}

    @staticmethod
    def _view(request):
        return {"request_id": request["request_id"], "run_id": request["run_id"], "status": request["status"],
                "policy_digest": request["policy_digest"], "payload_digest": request["payload_digest"],
                "preview": request["preview"], "decision": request["decision"], "receipt": request["receipt"]}

    def inspect_request(self, session, run_id, request_id):
        with self._transaction(session, allow_paused=True) as (db, _, actor):
            self.store._row(db, run_id, lock=True)
            request = self._request(db, request_id, run_id)
            if request["requester"] != actor["actor_id"] and not {"REVIEWER", "OPERATOR"}.intersection(actor["roles"]):
                raise AdmissionDenied("ROLE_DENIED")
            return self._view(request)

    def decide(self, session, run_id, request_id, payload_digest, current_policy, decision, *, seconds=600):
        integer(seconds, minimum=1, maximum=BROKER_POLICY["approval_seconds"])
        if decision not in ("APPROVE", "REJECT"):
            raise AdmissionDenied("INVALID_DECISION")
        with self._transaction(session, "REVIEWER") as (db, policy, actor):
            _, record, target = self._run(db, run_id)
            request = self._request(db, request_id, run_id)
            self._match_request(request, record, policy, target)
            if payload_digest != request["payload_digest"] or current_policy != request["policy_digest"]:
                raise AdmissionDenied("PAYLOAD_OR_POLICY_CHANGED")
            if actor["actor_id"] == request["requester"]:
                raise AdmissionDenied("SELF_APPROVAL_DENIED")
            if request["status"] != "PENDING_HUMAN":
                previous = request["decision"]
                if request["status"] in ("APPROVED", "REJECTED", "SIMULATED") and previous["actor_id"] == actor["actor_id"] and previous["decision"] == decision:
                    return previous  # Historical decision only; never extends authority.
                raise AdmissionDenied("DECISION_FINAL")
            if not request["pending_live"]:
                raise AdmissionDenied("REQUEST_EXPIRED")
            expiry = db.execute("SELECT LEAST(clock_timestamp()+(%s * interval '1 second'),%s::timestamptz,%s::timestamptz) AS expiry",
                                (seconds, actor["expires_at"], request["expires_at"])).fetchone()["expiry"]
            approval = {"schema_version": PROTOCOL_VERSION, "approval_id": str(uuid4()), "actor_id": actor["actor_id"],
                        "run_id": run_id, "binding_digest": digest(record["binding"]), "payload_digest": payload_digest,
                        "policy_digest": current_policy, "action": "SIMULATE_COMMENT", "decision": decision,
                        "expires_at": expiry.isoformat()}
            validate_record("SimulationApproval", approval)
            db.execute("""UPDATE broker_requests SET status=%s,decision=%s,issuer_session=%s,issuer_version=%s
                WHERE request_id=%s""", ("APPROVED" if decision == "APPROVE" else "REJECTED", Jsonb(approval), actor["token_hash"], actor["version"], request_id))
            self._audit(db, actor, decision, run_id, request_id=request_id, payload_digest=payload_digest)
            return approval

    def revoke_approval(self, reviewer, run_id, request_id):
        with self._transaction(reviewer, "REVIEWER", allow_paused=True) as (db, _, actor):
            self.store._row(db, run_id, lock=True)
            request = self._request(db, request_id, run_id)
            if not request["decision"] or request["decision"]["actor_id"] != actor["actor_id"] or request["status"] not in ("APPROVED", "REJECTED"):
                raise AdmissionDenied("DECISION_FINAL")
            db.execute("UPDATE broker_requests SET status='REVOKED' WHERE request_id=%s", (request_id,))
            self._audit(db, actor, "APPROVAL_REVOKED", run_id, request_id=request_id)

    def usage(self, session, run_id):
        with self._transaction(session, allow_paused=True) as (db, _, _):
            self.store._row(db, run_id, lock=True)
            row = db.execute("SELECT accepted FROM broker_usage WHERE run_id=%s", (run_id,)).fetchone()
            return row["accepted"] if row else 0

    def controller_store(self, session):
        """Trusted host facade: only model admission is a broker tool here.

        Lifecycle create/load/save/lease are privileged kernel calls. Do not
        expose this facade or its parent to specialists or service clients.
        """
        return BrokeredControllerStore(self, session)


class BrokeredControllerStore:
    def __init__(self, broker, session, leased=None):
        self.broker, self.session, self.leased = broker, session, leased

    def create(self, record):
        self.broker.store.create(record)

    def load(self, run_id):
        return (self.leased or self.broker.store).load(run_id)

    def save(self, record, expected_version, event):
        if self.leased is None:
            raise AdmissionDenied("LEASE_REQUIRED")
        return self.leased.save(record, expected_version, event)

    @property
    def worker(self):
        # The controller detects this attribute; leased facades must not expose
        # it or recursively acquire another lease.
        if self.leased is not None:
            raise AttributeError("leased facade has no worker acquisition")
        return self._worker

    @contextmanager
    def _worker(self, run_id):
        with self.broker.store.worker(run_id) as leased:
            yield BrokeredControllerStore(self.broker, self.session, leased)

    def admit_model(self, record):
        if self.leased is None:
            raise AdmissionDenied("LEASE_REQUIRED")
        context = self.broker.context(self.session)
        proposal = action_proposal(record, context["policy_digest"], "MOCK_REVIEW", {})
        return self.broker.execute(self.session, proposal, lease=self.leased.token)
