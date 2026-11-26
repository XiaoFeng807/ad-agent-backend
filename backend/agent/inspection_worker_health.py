"""Compose entry point and health probe for the background inspection loop."""

import argparse
from pathlib import Path
from time import monotonic


HEARTBEAT = Path("/run/storepilot-inspection/heartbeat")
INTERVAL = 2
MAX_AGE = 60


class Heartbeat:
    def __init__(self, path=HEARTBEAT):
        self.path = path
        self.next_write = 0

    def reset(self):
        self.path.unlink(missing_ok=True)

    def tick(self):
        now = monotonic()
        if now >= self.next_write:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(str(now), encoding="ascii")
            temporary.replace(self.path)
            self.next_write = now + INTERVAL

    def check(self):
        try:
            age = monotonic() - float(self.path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return "worker has not entered the polling loop"
        if not 0 <= age <= MAX_AGE:
            return "worker polling heartbeat is stale"
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    heartbeat = Heartbeat()
    if args.check:
        if reason := heartbeat.check():
            raise SystemExit(reason)
        return

    heartbeat.reset()
    from backend.agent.background_inspection import serve

    serve(on_poll=heartbeat.tick)


if __name__ == "__main__":
    main()
