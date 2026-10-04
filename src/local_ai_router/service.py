"""User-level hub lifecycle. Stop uses authenticated service ownership, never a guessed PID."""

import os
import subprocess
import sys
import time

import httpx


def url(settings):
    host = "[::1]" if settings.host == "::1" else settings.host
    return f"http://{host}:{settings.port}"


def status(settings):
    try:
        # Windows may take just over two seconds to refuse an unused loopback
        # port. Allow that refusal so a stopped hub can still be started.
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=4) as client:
            response = client.get(url(settings) + "/health")
            if response.status_code != 200 or response.json().get("service") != "local-ai-router":
                raise RuntimeError("The configured port belongs to another service")
            return {**response.json(), "endpoint": url(settings), "running": True}
    except httpx.ConnectError:
        return {"status": "stopped", "running": False, "endpoint": url(settings)}


def stop(settings):
    if not status(settings)["running"]:
        return {"status": "stopped", "running": False}
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=3) as client:
        response = client.post(url(settings) + "/router/stop", headers={"Authorization": "Bearer " + settings.token})
        response.raise_for_status()
    for _ in range(100):
        time.sleep(0.1)
        if not status(settings)["running"]:
            return {"status": "stopped", "running": False}
    raise RuntimeError("Service is still draining work; check accs status before restarting")


def start(settings):
    state = status(settings)
    if state["running"]:
        return {**state, "already_running": True}
    command = [sys.executable, "-m", "local_ai_router.cli", "--home", str(settings.home)]
    if settings.mock:
        command.append("--mock")
    command += ["serve", "--port", str(settings.port), "--host", settings.host]
    path = settings.state / "service.log"
    with path.open("ab") as log:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
            close_fds=True,
        )
    path.chmod(0o600)
    for _ in range(100):
        if child.poll() is not None:
            raise RuntimeError("Service could not start; inspect " + str(path))
        time.sleep(0.1)
        state = status(settings)
        if state["running"]:
            return {**state, "pid": child.pid, "log": str(path)}
    raise RuntimeError("Service is still starting; inspect accs status and " + str(path))
