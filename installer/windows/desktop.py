"""Start the installed hub and open Workbench using the bundled Python runtime."""

import argparse
import ctypes
import os
import sys
import webbrowser

from local_ai_router.config import load
from local_ai_router.service import start, stop, url


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open Accension Workbench")
    parser.add_argument("--home", help="Configuration home; defaults to the user's Accension profile")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = load(args.home)
        if args.port is not None:
            settings = type(settings).model_validate({**settings.model_dump(), "port": args.port})
        if args.stop:
            stop(settings)
        else:
            start(settings)
            if not args.no_browser:
                if not webbrowser.open(url(settings) + "/ui/workbench"):
                    raise RuntimeError("Browser could not open")
        return 0
    except Exception:
        message = (
            "Accension could not stop." if args.stop else "Accension could not open."
        ) + " Check that port is available and inspect the service.log in your Accension profile's .router folder."
        if os.name == "nt" and not args.no_browser and not args.stop:
            ctypes.windll.user32.MessageBoxW(None, message, "Accension", 0x10)
        elif sys.stderr is not None:
            print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
