"""Serve the real homepage over loopback using only synthetic business rows."""

from contextlib import closing
from pathlib import Path
import socket
import sys
from unittest.mock import patch

import uvicorn

from backend.agent.llm_provider import ProviderFactory
from backend.agent.tests.test_home_plan_scope import AccountModel, seed_scope
from backend.database import database
from tests.home_diagnosis_server import demo_captcha


def serve(directory):
    database.DB_PATH = directory / "scope.sqlite3"
    database.init_db()
    with closing(database.get_db()) as conn:
        seed_scope(conn)

    with (patch.object(ProviderFactory, "create", side_effect=lambda *args, **kwargs: AccountModel()),
          patch("backend.captcha.captcha.generate_captcha", demo_captcha), socket.socket() as listener):
        from fast_server import app

        listener.bind(("127.0.0.1", 0))
        listener.listen()
        print(f"DEMO_URL=http://127.0.0.1:{listener.getsockname()[1]}", flush=True)
        uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning")).run(sockets=[listener])


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
