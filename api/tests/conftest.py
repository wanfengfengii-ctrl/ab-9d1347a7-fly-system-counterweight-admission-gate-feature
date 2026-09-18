import os

import httpx
import pytest

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(autouse=True)
def reset_battens(base_url):
    """每个验收场景开始前，恢复 G-01 / G-02 两根空吊杆。"""
    resp = httpx.post(f"{base_url}/api/reset", timeout=10)
    assert resp.status_code == 200
    yield
