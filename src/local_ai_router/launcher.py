"""Open the loopback UI; browser sessions require no user-entered credentials."""
import socket
import threading
import time
import webbrowser
import httpx
from .service import url as service_url


def announce(settings, no_browser):
    url = service_url(settings) + "/ui"
    print("Accension: " + url, flush=True)
    if not no_browser:
        def ready():
            for _ in range(50):
                try:
                    with socket.create_connection((settings.host, settings.port), timeout=.2):
                        webbrowser.open(url)
                        return
                except OSError:
                    time.sleep(.1)
        threading.Thread(target=ready, daemon=True).start()


def existing_ui(settings, no_browser):
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=2) as client:
            url = service_url(settings)
            result = client.get(url + "/health")
            if result.status_code != 200 or result.json().get("service") != "local-ai-router":
                raise ValueError("Port is occupied by another service; choose accs ui --port NUMBER")
            announce(settings, no_browser)
            return True
    except httpx.ConnectError:
        return False


def launch(app, settings, no_browser):
    announce(settings, no_browser)
