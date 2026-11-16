"""Serve the real homepage with the business scope fixture on loopback."""

from contextlib import closing
from pathlib import Path
import socket
import sys
from unittest.mock import patch

import uvicorn

from backend.agent.tests.test_home_business_scope import FixedModel, ProviderFactory, seed_business
from backend.database import database
from tests.home_diagnosis_server import demo_captcha


def serve(directory):
    database.DB_PATH = directory / "business.sqlite3"
    database.init_db()
    with closing(database.get_db()) as conn:
        seed_business(conn)
        with conn:
            conn.executemany("INSERT INTO alerts (user_id, type, message) VALUES (?, 'demo', ?)", [
                (17, "first-alert"), (18, "second-alert-one"), (18, "second-alert-two"),
            ])
            conn.executemany("INSERT INTO operation_logs (user_id, action) VALUES (?, ?)", [
                (17, "first-log"), (18, "second-log"),
            ])
    with (patch.object(ProviderFactory, "create", side_effect=lambda *a, **kw: FixedModel()),
          patch("backend.captcha.captcha.generate_captcha", demo_captcha), socket.socket() as listener):
        from fast_server import app

        listener.bind(("127.0.0.1", 0))
        listener.listen()
        print(f"DEMO_URL=http://127.0.0.1:{listener.getsockname()[1]}", flush=True)
        uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning")).run(sockets=[listener])


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
