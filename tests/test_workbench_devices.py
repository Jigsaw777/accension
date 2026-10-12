"""Explicit pairing, race winner, revocation and secret-free persisted state."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from test_workbench_agents import HUMAN

from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.store import WorkbenchError, identity


@pytest.fixture
async def devices(engine):
    runtime = WorkbenchRuntime(engine)
    ws = runtime.service.seed_defaults()
    runtime.service.update(
        "workspace",
        ws.id,
        ws.id,
        {"command_id": identity(), "expected_revision": ws.revision, "privacy_mode": "PRIVATE_LAN"},
        HUMAN,
    )
    engine.settings.workbench_lan.workspaces = [ws.id]
    yield runtime.devices, ws
    await runtime.close()


def pending(devices):
    service, ws = devices
    invitation = service.invitation(ws.id, HUMAN)
    request = service.request(invitation["code"], "Test device", "source")
    return invitation, request


def approved(devices):
    service, ws = devices
    invitation, request = pending(devices)
    service.approve(request["id"], ws.id, ["workspace.read", "artifact.read"], request["verification_phrase"], HUMAN)
    return invitation, request


async def test_pairing_requires_local_approval(devices):
    service, ws = devices
    _, request = pending(devices)
    with pytest.raises(WorkbenchError, match="approval"):
        service.claim(request["id"], request["challenge"], True, "source")
    service.approve(request["id"], ws.id, ["workspace.read"], request["verification_phrase"], HUMAN)
    result = service.claim(request["id"], request["challenge"], True, "source")
    principal, _ = service.authenticate(result["token"], True)
    principal.authorize(ws.id, "workspace.read")
    with pytest.raises(WorkbenchError):
        principal.authorize(ws.id, "policy.admin")


async def test_two_claims_one_credential(devices):
    service, _ = devices
    _, request = approved(devices)

    def claim(_):
        try:
            return service.claim(request["id"], request["challenge"], False, "source")
        except WorkbenchError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, range(2)))
    assert len([r for r in results if r]) == 1
    assert service.store.db.execute("SELECT COUNT(*) FROM wb_device_credentials").fetchone()[0] == 1


async def test_wrong_challenge_attempt_cap_is_durable(devices):
    service, _ = devices
    _, request = approved(devices)
    for _ in range(5):
        with pytest.raises(WorkbenchError):
            service.claim(request["id"], "wrong" * 10, False, "challenge-attacker")
    row = service.store.db.execute("SELECT * FROM wb_pairings WHERE id=?", (request["id"],)).fetchone()
    assert row["attempts"] == 5 and row["state"] == "REJECTED"
    with pytest.raises(WorkbenchError):
        service.claim(request["id"], request["challenge"], False, "challenge-attacker")


async def test_invitation_secret_not_replayed_from_command(devices):
    from types import SimpleNamespace

    service, ws = devices

    class Command(SimpleNamespace):
        def model_dump(self, **kwargs):
            return vars(self)

    command = Command(action="invite", command_id=identity())
    issued = service.operator(ws.id, command, HUMAN)
    assert issued.body["code"]
    with pytest.raises(WorkbenchError, match="new invitation"):
        service.operator(ws.id, command, HUMAN)
    assert issued.body["code"] not in "\n".join(service.store.db.iterdump())


async def test_pairing_expiry_bruteforce_limits(devices):
    service, _ = devices
    invitation, request = pending(devices)
    stamp = service.clock()
    service.clock = lambda: stamp + 300001
    with pytest.raises(WorkbenchError):
        service.claim(request["id"], request["challenge"], False, "source")
    for _ in range(10):
        with pytest.raises(WorkbenchError):
            service.request("x" * 32, "Bad", "attacker")
    with pytest.raises(WorkbenchError, match="rate"):
        service.request("x" * 32, "Bad", "attacker")


async def test_no_plaintext_secrets_in_db_or_logs(devices):
    service, _ = devices
    invitation, request = approved(devices)
    result = service.claim(request["id"], request["challenge"], True, "source")
    dump = "\n".join(service.store.db.iterdump())
    for secret in (invitation["code"], request["challenge"], result["token"].split(".")[1], result["csrf"]):
        assert secret not in dump


async def test_revocation_one_device_and_rotation(devices):
    service, ws = devices
    _, request = approved(devices)
    one = service.claim(request["id"], request["challenge"], False, "source")
    _, request = approved(devices)
    two = service.claim(request["id"], request["challenge"], True, "source")
    first, _ = service.authenticate(one["token"])
    second, _ = service.authenticate(two["token"], True)
    rotated = service.rotate(first)
    with pytest.raises(WorkbenchError):
        service.authenticate(one["token"])
    first, _ = service.authenticate(rotated["token"])
    service.revoke(ws.id, one["device_id"], 2, HUMAN)
    with pytest.raises(WorkbenchError):
        first.authorize(ws.id, "workspace.read")
    second.authorize(ws.id, "workspace.read")
