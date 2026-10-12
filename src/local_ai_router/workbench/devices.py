"""Host-approved one-use pairing and digest-only device identities."""

import hashlib
import hmac
import json
import secrets
from collections import defaultdict, deque
from contextlib import contextmanager, nullcontext

from .events import append_event
from .policy import Principal, authorize
from .schema import ActorType, DeviceProjection
from .store import WorkbenchError, identity, now

REMOTE_CAPABILITIES = frozenset(
    {
        "workspace.read",
        "project.write",
        "prd.write",
        "work.write",
        "comment.write",
        "assignment.write",
        "execution.request",
        "execution.approve",
        "review.approve",
        "artifact.read",
    }
)


class DevicePrincipal(Principal):
    def __init__(self, devices, device_id, credential_id, browser, capabilities, workspaces):
        super().__init__(
            "device-" + device_id,
            ActorType.HUMAN if browser else ActorType.MACHINE,
            frozenset(capabilities),
            frozenset(workspaces),
            False,
        )
        object.__setattr__(self, "devices", devices)
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "credential_id", credential_id)
        object.__setattr__(self, "browser", browser)

    def authorize(self, workspace_id, capability, *, local_only=False):
        device, _ = self.devices.current(self.device_id, self.credential_id, self.browser)
        grants = json.loads(device["grants_json"])
        if workspace_id not in grants:
            raise WorkbenchError("NOT_FOUND", "Workspace not found", 404)
        if local_only or capability not in grants[workspace_id]:
            raise WorkbenchError("FORBIDDEN", "Device action not granted", 403)
        if not self.browser and capability in {"execution.approve", "review.approve"}:
            raise WorkbenchError("FORBIDDEN", "Browser operator session required", 403)
        self.devices.grants(workspace_id, grants[workspace_id])


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


class Devices:
    def __init__(self, runtime, clock=now):
        self.runtime, self.store = runtime, runtime.service.store
        self.clock = clock
        self.rates = defaultdict(deque)

    @contextmanager
    def transaction(self):
        # Join only the command service transaction held by this same thread.
        with self.store.router.lock:
            with nullcontext() if self.store.db.in_transaction else self.store.router.transaction():
                yield

    def system(self, workspace_id):
        return Principal("pairing", ActorType.SYSTEM, REMOTE_CAPABILITIES, frozenset({workspace_id}), True)

    def operator(self, workspace_id, command, principal):
        from .service import CommandResult

        issued = {}

        def apply():
            if command.action == "invite":
                issued.update(self.invitation(workspace_id, principal))
                body = {key: value for key, value in issued.items() if key != "code"}
            elif command.action in {"approve", "deny"}:
                body = self.approve(
                    command.id,
                    workspace_id,
                    command.capabilities,
                    command.phrase,
                    principal,
                    command.action == "deny",
                    command.expected_revision,
                )
            else:
                body = self.revoke(workspace_id, command.id, command.expected_revision, principal)
            return CommandResult(200, body)

        result = self.runtime.service.command(
            principal,
            workspace_id,
            "device.admin",
            command.command_id,
            "device_" + command.action,
            command.model_dump(mode="json"),
            apply,
            local_only=True,
        )
        if command.action == "invite":
            if result.replayed:
                raise WorkbenchError("SECRET_NOT_REPLAYABLE", "Issue a new invitation after a lost response", 409)
            return CommandResult(200, issued)
        return result

    def rate(self, source):
        stamp = self.clock()
        for key, limit in (("global", 100), (source, 10)):
            bucket = self.rates[key]
            while bucket and bucket[0] <= stamp - 60000:
                bucket.popleft()
            if len(bucket) >= limit:
                raise WorkbenchError("RATE_LIMIT", "Pairing rate exceeded", 429)
        self.rates["global"].append(stamp)
        self.rates[source].append(stamp)
        if len(self.rates) > 1000:
            for key in list(self.rates):
                if not self.rates[key] or self.rates[key][-1] <= stamp - 60000:
                    del self.rates[key]

    def current(self, device_id, credential_id, browser):
        with self.store.router.lock:
            device = self.store.db.execute("SELECT * FROM wb_devices WHERE id=?", (device_id,)).fetchone()
            table = "wb_device_sessions" if browser else "wb_device_credentials"
            credential = self.store.db.execute(
                f"SELECT * FROM {table} WHERE id=? AND device_id=?", (credential_id, device_id)
            ).fetchone()
            if (
                device is None
                or device["state"] != "ACTIVE"
                or credential is None
                or credential["revoked_at"] is not None
                or credential["expires_at"] <= self.clock()
            ):
                raise WorkbenchError("UNAUTHENTICATED", "Device credential expired or revoked", 401)
            return device, credential

    def authenticate(self, token, browser=False):
        if not isinstance(token, str) or len(token) > 200 or token.count(".") != 1:
            raise WorkbenchError("UNAUTHENTICATED", "Device credential required", 401)
        credential_id, secret = token.split(".")
        table = "wb_device_sessions" if browser else "wb_device_credentials"
        row = self.store.db.execute(f"SELECT * FROM {table} WHERE id=?", (credential_id,)).fetchone()
        if row is None or not hmac.compare_digest(row["secret_digest"], digest(secret)):
            raise WorkbenchError("UNAUTHENTICATED", "Invalid device credential", 401)
        device, _ = self.current(row["device_id"], credential_id, browser)
        grants = json.loads(device["grants_json"])
        capabilities = set().union(*(set(c) for c in grants.values()))
        if not browser:
            capabilities -= {"execution.approve", "review.approve"}
        principal = DevicePrincipal(self, device["id"], credential_id, browser, capabilities, grants)
        return principal, row

    def listing(self, workspace_id, principal, pending=False):
        authorize(principal, workspace_id, "device.admin", local_only=True)
        if pending:
            rows = self.store.db.execute(
                "SELECT id,client_name,phrase,state,expires_at,revision FROM wb_pairings WHERE workspace_id=? AND state='PENDING_APPROVAL' AND expires_at>? ORDER BY expires_at LIMIT 32",
                (workspace_id, self.clock()),
            ).fetchall()
        else:
            rows = [
                r
                for r in self.store.db.execute(
                    "SELECT id,name,state,grants_json,revision,created_at,revoked_at FROM wb_devices WHERE EXISTS(SELECT 1 FROM json_each(wb_devices.grants_json) WHERE key=?) ORDER BY id LIMIT 200",
                    (workspace_id,),
                )
                if workspace_id in json.loads(r["grants_json"])
            ]
        return {"items": [dict(r) for r in rows]}

    def revoke(self, workspace_id, device_id, expected_revision, principal):
        authorize(principal, workspace_id, "device.admin", local_only=True)
        with self.transaction():
            row = self.store.db.execute("SELECT * FROM wb_devices WHERE id=?", (device_id,)).fetchone()
            if row is None or workspace_id not in json.loads(row["grants_json"]):
                raise WorkbenchError("NOT_FOUND", "Device not found", 404)
            if row["revision"] != expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Device changed")
            self.store.db.execute(
                "UPDATE wb_devices SET state='REVOKED',revoked_at=?,revision=revision+1 WHERE id=?",
                (self.clock(), device_id),
            )
            for table in ("wb_device_credentials", "wb_device_sessions"):
                self.store.db.execute(f"UPDATE {table} SET revoked_at=? WHERE device_id=?", (self.clock(), device_id))
            self.store.db.execute(
                "UPDATE wb_execution_grants SET revoked_at=? WHERE principal_id=? AND revoked_at IS NULL",
                (self.clock(), "device-" + device_id),
            )
            self.store.db.execute("DELETE FROM wb_snapshot_leases WHERE principal_id=?", ("device-" + device_id,))
            self.event(device_id, workspace_id, row["name"], "REVOKED", row["revision"] + 1, principal)
        return {"id": device_id, "state": "REVOKED", "revision": row["revision"] + 1}

    def rotate(self, principal):
        if principal.browser:
            raise WorkbenchError("FORBIDDEN", "Browser requires pairing for a new session", 403)
        secret = secrets.token_urlsafe(32)
        cid = identity()
        with self.transaction():
            self.current(principal.device_id, principal.credential_id, False)
            self.store.db.execute(
                "UPDATE wb_device_credentials SET revoked_at=? WHERE id=?", (self.clock(), principal.credential_id)
            )
            self.store.db.execute(
                "INSERT INTO wb_device_credentials VALUES(?,?,?,?,?,NULL)",
                (cid, principal.device_id, digest(secret), self.clock(), self.clock() + 30 * 86400000),
            )
            device = self.store.db.execute("SELECT * FROM wb_devices WHERE id=?", (principal.device_id,)).fetchone()
            self.store.db.execute("UPDATE wb_devices SET revision=revision+1 WHERE id=?", (principal.device_id,))
            for workspace_id in json.loads(device["grants_json"]):
                self.event(device["id"], workspace_id, device["name"], "ACTIVE", device["revision"] + 1, principal)
        return {"token": cid + "." + secret}

    def grants(self, workspace_id, capabilities):
        workspace = self.store.get("workspace", workspace_id, workspace_id)
        if (
            workspace.privacy_mode == "DEVICE_ONLY"
            or workspace_id not in self.runtime.engine.settings.workbench_lan.workspaces
        ):
            raise WorkbenchError("WORKSPACE_LOCAL_ONLY", "Workspace is not permitted on LAN", 403)
        if (
            not isinstance(capabilities, list)
            or set(capabilities) - REMOTE_CAPABILITIES
            or "workspace.read" not in capabilities
        ):
            raise WorkbenchError("INVALID_GRANTS", "Explicit remote capabilities require workspace.read", 422)
        return {workspace_id: sorted(set(capabilities))}

    def event(self, device_id, workspace_id, name, state, revision, principal):
        append_event(
            self.store,
            "device",
            DeviceProjection(id=device_id, workspace_id=workspace_id, name=name, state=state, revision=revision),
            principal,
            "device_" + state.lower(),
        )

    def invitation(self, workspace_id, principal):
        authorize(principal, workspace_id, "device.admin", local_only=True)
        grants = self.grants(workspace_id, ["workspace.read", "artifact.read"])
        code = secrets.token_urlsafe(24)
        pid = identity()
        with self.transaction():
            for expired in self.store.db.execute(
                "SELECT * FROM wb_pairings WHERE expires_at<=? AND state IN ('ISSUED','PENDING_APPROVAL','APPROVED') LIMIT 32",
                (self.clock(),),
            ).fetchall():
                self.store.db.execute(
                    "UPDATE wb_pairings SET state='EXPIRED',revision=revision+1 WHERE id=?", (expired["id"],)
                )
                self.event(
                    expired["id"],
                    expired["workspace_id"],
                    expired["client_name"] or "Invitation",
                    "EXPIRED",
                    expired["revision"] + 1,
                    principal,
                )
            if (
                self.store.db.execute(
                    "SELECT COUNT(*) FROM wb_pairings WHERE state IN ('ISSUED','PENDING_APPROVAL','APPROVED')"
                ).fetchone()[0]
                >= 32
            ):
                raise WorkbenchError("PAIRING_LIMIT", "Too many pending pairings", 429)
            self.store.db.execute(
                "INSERT INTO wb_pairings(id,workspace_id,code_digest,state,grants_json,expires_at,attempts,revision) VALUES(?,?,?,'ISSUED',?,?,0,1)",
                (pid, workspace_id, digest(code), json.dumps(grants), self.clock() + 300000),
            )
            self.event(pid, workspace_id, "Invitation", "ISSUED", 1, principal)
        return {
            "id": pid,
            "code": code,
            "expires_in_seconds": 300,
            "urls": self.runtime.engine.settings.workbench_lan.origins,
        }

    def request(self, code, name, source):
        if (
            not isinstance(code, str)
            or not 20 <= len(code) <= 128
            or not isinstance(name, str)
            or not 1 <= len(name) <= 80
        ):
            raise WorkbenchError("INVALID_PAIRING", "Invalid pairing input", 422)
        challenge = secrets.token_urlsafe(32)
        with self.transaction():
            self.rate(source)
            row = self.store.db.execute("SELECT * FROM wb_pairings WHERE code_digest=?", (digest(code),)).fetchone()
            if row is None or row["state"] != "ISSUED" or row["expires_at"] <= self.clock():
                raise WorkbenchError("INVALID_PAIRING", "Pairing unavailable", 403)
            phrase = secrets.token_hex(4)
            self.store.db.execute(
                "UPDATE wb_pairings SET challenge_digest=?,client_name=?,phrase=?,state='PENDING_APPROVAL',revision=revision+1 WHERE id=?",
                (digest(challenge), name, phrase, row["id"]),
            )
            self.event(
                row["id"],
                row["workspace_id"],
                name,
                "PENDING_APPROVAL",
                row["revision"] + 1,
                self.system(row["workspace_id"]),
            )
        return {"id": row["id"], "challenge": challenge, "verification_phrase": phrase, "status": "PENDING_APPROVAL"}

    def pairing(self, pid, challenge, *, check_only=False):
        invalid = False
        with self.transaction():
            row = self.store.db.execute("SELECT * FROM wb_pairings WHERE id=?", (pid,)).fetchone()
            invalid = (
                row is None
                or not isinstance(challenge, str)
                or row["attempts"] >= 5
                or not hmac.compare_digest(row["challenge_digest"] or "", digest(challenge))
                or row["expires_at"] <= self.clock()
            )
            if (
                invalid
                and row is not None
                and not check_only
                and row["state"] in {"ISSUED", "PENDING_APPROVAL", "APPROVED"}
            ):
                state = (
                    "EXPIRED"
                    if row["expires_at"] <= self.clock()
                    else ("REJECTED" if row["attempts"] >= 4 else row["state"])
                )
                self.store.db.execute(
                    "UPDATE wb_pairings SET attempts=attempts+1,state=?,revision=revision+1 WHERE id=?", (state, pid)
                )
                self.event(
                    pid,
                    row["workspace_id"],
                    row["client_name"] or "Invitation",
                    state,
                    row["revision"] + 1,
                    self.system(row["workspace_id"]),
                )
        if invalid:
            raise WorkbenchError("INVALID_PAIRING", "Challenge expired or invalid", 403)
        return row

    def approve(self, pid, workspace_id, capabilities, phrase, principal, reject=False, expected_revision=None):
        authorize(principal, workspace_id, "device.admin", local_only=True)
        with self.transaction():
            row = self.store.db.execute(
                "SELECT * FROM wb_pairings WHERE id=? AND workspace_id=?", (pid, workspace_id)
            ).fetchone()
            if (
                row is None
                or row["state"] != "PENDING_APPROVAL"
                or row["expires_at"] <= self.clock()
                or phrase != row["phrase"]
            ):
                raise WorkbenchError("INVALID_PAIRING", "Compare current client phrase before approval")
            if expected_revision is not None and row["revision"] != expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Pairing changed")
            grants = self.grants(workspace_id, capabilities)
            state = "REJECTED" if reject else "APPROVED"
            self.store.db.execute(
                "UPDATE wb_pairings SET state=?,grants_json=?,approved_by=?,approved_at=?,revision=revision+1 WHERE id=?",
                (state, json.dumps(grants), principal.id, self.clock(), pid),
            )
            self.event(pid, workspace_id, row["client_name"], state, row["revision"] + 1, principal)
        return {"id": pid, "status": state}

    def claim(self, pid, challenge, browser, source):
        secret = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self.transaction():
            self.rate(source)
        self.pairing(pid, challenge)
        with self.transaction():
            row = self.pairing(pid, challenge, check_only=True)
            if row["state"] != "APPROVED":
                raise WorkbenchError("PAIRING_NOT_APPROVED", "Host approval required", 403)
            device, credential = identity(), identity()
            self.store.db.execute(
                "INSERT INTO wb_devices VALUES(?,?,'ACTIVE',?,1,?,NULL)",
                (device, row["client_name"], row["grants_json"], self.clock()),
            )
            if browser:
                self.store.db.execute(
                    "INSERT INTO wb_device_sessions VALUES(?,?,NULL,?,?,?,?,NULL)",
                    (credential, device, digest(secret), digest(csrf), self.clock() + 8 * 3600000, self.clock()),
                )
            else:
                self.store.db.execute(
                    "INSERT INTO wb_device_credentials VALUES(?,?,?,?,?,NULL)",
                    (credential, device, digest(secret), self.clock(), self.clock() + 30 * 86400000),
                )
            self.store.db.execute(
                "UPDATE wb_pairings SET state='CONSUMED',consumed_at=?,revision=revision+1 WHERE id=?",
                (self.clock(), pid),
            )
            principal = self.system(row["workspace_id"])
            self.event(pid, row["workspace_id"], row["client_name"], "CONSUMED", row["revision"] + 1, principal)
            self.event(device, row["workspace_id"], row["client_name"], "ACTIVE", 1, principal)
        return {
            "device_id": device,
            "token": credential + "." + secret,
            "csrf": csrf if browser else None,
            "grants": json.loads(row["grants_json"]),
        }
