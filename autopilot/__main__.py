"""Command line.

  python -m autopilot demo          copy the sample documents into the inbox and process them
  python -m autopilot run           process everything in the inbox once
  python -m autopilot watch         keep watching the inbox
  python -m autopilot email         pull unread emails over IMAP into the inbox, then process
  python -m autopilot retry         retry failed documents
  python -m autopilot serve         start the dashboard on http://127.0.0.1:8000
  python -m autopilot metrics       print the dashboard numbers as JSON
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from .config import ROOT
from .pipeline import Autopilot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autopilot", description="Ops Autopilot")
    parser.add_argument("command", choices=["demo", "run", "watch", "email", "retry", "serve", "metrics"])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)

    pilot = Autopilot()
    mode = f"live model {pilot.settings.model}" if pilot.live else "offline replay (no API key found)"
    print(f"Ops Autopilot · {mode} · data in {pilot.settings.data_dir}")

    if args.command == "demo":
        for src in sorted((ROOT / "evals" / "cases").iterdir()):
            if src.is_file():
                shutil.copy2(src, pilot.settings.inbox_dir / src.name)
        args.command = "run"

    if args.command == "email":
        from .triggers import fetch_imap

        saved = fetch_imap(pilot.settings.inbox_dir)
        print(f"Saved {len(saved)} email(s) to the inbox")
        args.command = "run"

    if args.command == "run":
        for row in pilot.run_inbox():
            print(f"  {row['status']:<13} {row['filename']}  {row.get('reason') or row.get('error') or ''}")
        print(json.dumps(pilot.metrics(), indent=2))
    elif args.command == "watch":
        from .triggers import watch_folder

        watch_folder(pilot, interval=args.interval)
    elif args.command == "retry":
        for row in pilot.retry_failed():
            print(f"  {row['status']:<13} {row['filename']}")
    elif args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(pilot), host="127.0.0.1", port=args.port)
    elif args.command == "metrics":
        print(json.dumps(pilot.metrics(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
