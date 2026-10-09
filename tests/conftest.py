"""Test fixtures: the API is started as a real uvicorn process (like the grader does) against a throw-away
database, and talked to over HTTP. Needs a MongoDB 6+ reachable at MONGO_URI (default localhost:27017)."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
TEST_DB = "attendance_test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mongo_db():
    client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    return client[TEST_DB]


@pytest.fixture(scope="session")
def base_url(mongo_db):
    mongo_db.client.drop_database(TEST_DB)
    port = _free_port()
    env = {**os.environ, "MONGO_URI": MONGO_URI, "MONGO_DB": TEST_DB}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
        env=env,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(url + "/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("API did not become healthy")
    yield url
    proc.terminate()
    proc.wait(10)


@pytest.fixture()
def api(base_url, mongo_db):
    """httpx client + empty collections (documents are deleted, indexes stay)."""
    mongo_db.employees.delete_many({})
    mongo_db.attendance_logs.delete_many({})
    with httpx.Client(base_url=base_url, timeout=20) as c:
        yield c


@pytest.fixture()
def db(mongo_db):
    return mongo_db
