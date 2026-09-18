"""真实 HTTP + 真实 PostgreSQL 的端到端验收，不使用任何假接口。"""

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx


def load_piece(base_url, batten_id, piece_id, weight_grams):
    return httpx.post(
        f"{base_url}/api/battens/{batten_id}/loads",
        json={"piece_id": piece_id, "weight_grams": weight_grams},
        timeout=30,
    )


def batten_state(base_url, batten_id):
    resp = httpx.get(f"{base_url}/api/battens/{batten_id}", timeout=10)
    assert resp.status_code == 200
    return resp.json()


def test_fixture_battens_are_restored_empty(base_url):
    data = httpx.get(f"{base_url}/api/battens", timeout=10).json()
    battens = {b["batten_id"]: b for b in data["battens"]}
    assert set(battens) == {"G-01", "G-02"}
    assert battens["G-01"]["capacity_grams"] == 30000
    assert battens["G-02"]["capacity_grams"] == 50000
    for batten in battens.values():
        assert batten["total_grams"] == 0
        assert batten["remaining_grams"] == batten["capacity_grams"]


def test_successful_load_returns_totals_and_is_stored(base_url):
    resp = load_piece(base_url, "G-01", "CW-0001", 20000)
    assert resp.status_code == 201
    body = resp.json()
    assert body["accepted"] is True
    assert body["total_grams"] == 20000
    assert body["remaining_grams"] == 10000
    assert body["load"]["piece_id"] == "CW-0001"

    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 20000
    assert state["remaining_grams"] == 10000
    assert [l["piece_id"] for l in state["loads"]] == ["CW-0001"]


def test_exact_capacity_is_allowed(base_url):
    assert load_piece(base_url, "G-01", "CW-0101", 25000).status_code == 201
    resp = load_piece(base_url, "G-01", "CW-0102", 5000)
    assert resp.status_code == 201
    body = resp.json()
    assert body["total_grams"] == 30000
    assert body["remaining_grams"] == 0


def test_over_capacity_is_rejected_and_not_persisted(base_url):
    assert load_piece(base_url, "G-01", "CW-0201", 20000).status_code == 201
    resp = load_piece(base_url, "G-01", "CW-0202", 10001)
    assert resp.status_code == 409
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "OVER_CAPACITY"

    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 20000
    assert state["remaining_grams"] == 10000
    # 失败请求不得写入：明细里只有第一笔
    assert [l["piece_id"] for l in state["loads"]] == ["CW-0201"]


def test_weight_bounds_are_enforced(base_url):
    for bad_weight in (0, 99, 25001, -100):
        resp = load_piece(base_url, "G-02", f"CW-BAD-{bad_weight}", bad_weight)
        assert resp.status_code == 422
        assert resp.json()["reason"] == "INVALID_WEIGHT"

    assert load_piece(base_url, "G-02", "CW-MIN", 100).status_code == 201
    assert load_piece(base_url, "G-02", "CW-MAX", 25000).status_code == 201

    state = batten_state(base_url, "G-02")
    assert state["total_grams"] == 100 + 25000
    assert len(state["loads"]) == 2  # 只有两笔成功记录落库


def test_non_integer_weight_is_rejected(base_url):
    """字符串、小数、布尔等伪装的重量必须明确拒绝且不保存。"""
    bad_payloads = ["100", "20000", 100.0, 199.5, True, None]
    for i, bad_weight in enumerate(bad_payloads):
        resp = httpx.post(
            f"{base_url}/api/battens/G-01/loads",
            json={"piece_id": f"CW-TYPE-{i}", "weight_grams": bad_weight},
            timeout=10,
        )
        assert resp.status_code == 422, f"载荷 {bad_weight!r} 未被拒绝: {resp.text}"
        body = resp.json()
        assert body["accepted"] is False
        assert body["reason"] == "INVALID_INPUT"

    # 全部被拒后吊杆必须仍是空的：失败请求不落库
    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 0
    assert state["loads"] == []


def test_piece_id_is_unique_across_all_battens(base_url):
    assert load_piece(base_url, "G-01", "CW-DUP", 5000).status_code == 201
    resp = load_piece(base_url, "G-02", "CW-DUP", 5000)
    assert resp.status_code == 409
    assert resp.json()["reason"] == "PIECE_ID_EXISTS"
    # 第二根吊杆上不留下任何失败痕迹
    assert batten_state(base_url, "G-02")["total_grams"] == 0
    assert batten_state(base_url, "G-02")["loads"] == []


def test_unknown_batten_is_rejected(base_url):
    resp = load_piece(base_url, "G-99", "CW-GHOST", 1000)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "BATTEN_NOT_FOUND"


def test_concurrent_loads_on_empty_batten_only_one_succeeds(base_url):
    """两台终端同时向空 G-01 登记各 20000 克：必须且仅能成功一笔。"""
    barrier = threading.Barrier(2)

    def submit(piece_id):
        barrier.wait(timeout=10)
        return load_piece(base_url, "G-01", piece_id, 20000)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ["CW-CONC-A", "CW-CONC-B"]))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 409], f"并发结果异常: {statuses}"

    winner = next(r for r in responses if r.status_code == 201).json()
    assert winner["accepted"] is True
    assert winner["total_grams"] == 20000
    assert winner["remaining_grams"] == 10000

    loser = next(r for r in responses if r.status_code == 409).json()
    assert loser["accepted"] is False
    assert loser["reason"] == "OVER_CAPACITY"

    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 20000
    assert state["remaining_grams"] == 10000
    assert len(state["loads"]) == 1
    assert state["loads"][0]["piece_id"] == winner["load"]["piece_id"]


def test_concurrent_same_piece_id_only_one_succeeds(base_url):
    """同一标识并发抢注：数据库唯一约束兜底，仅一笔成功。"""
    barrier = threading.Barrier(2)

    def submit(batten_id):
        barrier.wait(timeout=10)
        return load_piece(base_url, batten_id, "CW-RACE", 1000)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ["G-01", "G-02"]))

    assert sorted(r.status_code for r in responses) == [201, 409]
    loser = next(r for r in responses if r.status_code == 409).json()
    assert loser["reason"] == "PIECE_ID_EXISTS"
