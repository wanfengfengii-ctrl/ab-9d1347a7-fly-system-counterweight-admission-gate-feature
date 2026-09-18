"""配重片转移：真实 HTTP + 真实 PostgreSQL 端到端验收，不使用任何假接口。"""

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


def transfer(base_url, source_id, load_id, target_id):
    return httpx.post(
        f"{base_url}/api/battens/{source_id}/loads/{load_id}/transfer",
        json={"target_batten_id": target_id},
        timeout=30,
    )


def _load_id(detail, piece_id):
    return next(l["load_id"] for l in detail["loads"] if l["piece_id"] == piece_id)


def test_successful_transfer_moves_piece_and_preserves_weight_and_registration_time(base_url):
    created = load_piece(base_url, "G-01", "CW-MOVE", 20000)
    assert created.status_code == 201
    before = batten_state(base_url, "G-01")
    load_before = next(l for l in before["loads"] if l["piece_id"] == "CW-MOVE")

    resp = transfer(base_url, "G-01", load_before["load_id"], "G-02")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["source_batten_id"] == "G-01"
    assert body["target_batten_id"] == "G-02"
    assert body["load_id"] == load_before["load_id"]
    assert body["piece_id"] == "CW-MOVE"
    assert body["weight_grams"] == 20000

    # 响应直接给出转移后两根吊杆的总重 / 余量
    assert body["source"]["total_grams"] == 0
    assert body["source"]["remaining_grams"] == 30000
    assert body["target"]["total_grams"] == 20000
    assert body["target"]["remaining_grams"] == 30000

    # 刷新后以数据库为准：源杆清空、目标杆挂有同一笔记录
    src = batten_state(base_url, "G-01")
    dst = batten_state(base_url, "G-02")
    assert src["loads"] == []
    assert len(dst["loads"]) == 1
    moved = dst["loads"][0]
    assert moved["load_id"] == load_before["load_id"]
    assert moved["piece_id"] == "CW-MOVE"
    assert moved["weight_grams"] == 20000
    # 原始重量与登记时间必须保留，不是重新登记
    assert moved["created_at"] == load_before["created_at"]


def test_transfer_to_overfull_target_is_rejected_and_ownership_unchanged(base_url):
    load_piece(base_url, "G-01", "CW-STAY", 20000)
    # 目标杆先挂 35000 克，剩余 15000 克：20000 克的片子放不下
    load_piece(base_url, "G-02", "CW-OCC-A", 20000)
    load_piece(base_url, "G-02", "CW-OCC-B", 15000)
    source_before = batten_state(base_url, "G-01")
    load_id = _load_id(source_before, "CW-STAY")

    resp = transfer(base_url, "G-01", load_id, "G-02")
    assert resp.status_code == 409
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "OVER_CAPACITY"

    # 数据库保持原归属：片子仍在源杆，目标杆总重不变
    source_after = batten_state(base_url, "G-01")
    target_after = batten_state(base_url, "G-02")
    assert [l["piece_id"] for l in source_after["loads"]] == ["CW-STAY"]
    assert source_after["total_grams"] == 20000
    assert source_after["remaining_grams"] == 10000
    assert [l["piece_id"] for l in target_after["loads"]] == [
        "CW-OCC-A",
        "CW-OCC-B",
    ]
    assert target_after["total_grams"] == 35000
    assert target_after["remaining_grams"] == 15000


def test_transfer_when_position_already_changed_is_rejected(base_url):
    load_piece(base_url, "G-01", "CW-GONE", 5000)
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-GONE")

    # 另一终端先把片子移走
    first = transfer(base_url, "G-01", load_id, "G-02")
    assert first.status_code == 200

    # 旧界面仍以为它在 G-01，再次转移：明确返回当前位置已变化
    stale = transfer(base_url, "G-01", load_id, "G-02")
    assert stale.status_code == 409
    body = stale.json()
    assert body["accepted"] is False
    assert body["reason"] == "POSITION_CHANGED"
    assert "当前位置已变化" in body["message"]

    # 片子留在 G-02，未被重复搬动
    assert batten_state(base_url, "G-01")["loads"] == []
    dst = batten_state(base_url, "G-02")
    assert [l["piece_id"] for l in dst["loads"]] == ["CW-GONE"]


def test_transfer_to_same_batten_is_rejected(base_url):
    load_piece(base_url, "G-01", "CW-SELF", 5000)
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-SELF")

    resp = transfer(base_url, "G-01", load_id, "G-01")
    assert resp.status_code == 422
    assert resp.json()["reason"] == "SAME_BATTEN"

    detail = batten_state(base_url, "G-01")
    assert [l["piece_id"] for l in detail["loads"]] == ["CW-SELF"]


def test_transfer_to_unknown_target_is_rejected(base_url):
    load_piece(base_url, "G-01", "CW-T", 5000)
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-T")

    resp = transfer(base_url, "G-01", load_id, "G-99")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "BATTEN_NOT_FOUND"

    detail = batten_state(base_url, "G-01")
    assert [l["piece_id"] for l in detail["loads"]] == ["CW-T"]


def test_concurrent_opposite_transfers_leave_neither_batten_over_capacity(base_url):
    """两笔相反方向并发转移：固定顺序加锁串行裁决，两杆均不超限、不死锁。

    G-01 恰好满载 30000（A1 20000 + A2 10000），G-02 挂 B1 25000。
      T1: A2(10000) G-01 → G-02
      T2: B1(25000) G-02 → G-01
    无论锁队列按哪个方向先执行，B1 进入 G-01 都必须经过容量裁决
    （A2 已让出时 G-01 为 20000+25000=45000 仍超限，必须拒绝）。
    """
    load_piece(base_url, "G-01", "CW-A1", 20000)
    load_piece(base_url, "G-01", "CW-A2", 10000)
    load_piece(base_url, "G-02", "CW-B1", 25000)
    id_a2 = _load_id(batten_state(base_url, "G-01"), "CW-A2")
    id_b1 = _load_id(batten_state(base_url, "G-02"), "CW-B1")

    barrier = threading.Barrier(2)

    def submit(call):
        barrier.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                submit,
                [
                    lambda: transfer(base_url, "G-01", id_a2, "G-02"),
                    lambda: transfer(base_url, "G-02", id_b1, "G-01"),
                ],
            )
        )

    # 不死锁（无 500）、不多接纳：恰有一笔成功
    assert sorted(r.status_code for r in responses) == [200, 409]
    accepted = [r for r in responses if r.status_code == 200]
    rejected = [r for r in responses if r.status_code == 409]
    assert accepted[0].json()["piece_id"] == "CW-A2"
    assert rejected[0].json()["reason"] == "OVER_CAPACITY"

    g01 = batten_state(base_url, "G-01")
    g02 = batten_state(base_url, "G-02")
    # 固定加锁顺序保证不死锁；容量裁决保证两杆均不超限
    assert g01["total_grams"] <= g01["capacity_grams"]
    assert g02["total_grams"] <= g02["capacity_grams"]
    assert g01["remaining_grams"] >= 0
    assert g02["remaining_grams"] >= 0
    assert g01["total_grams"] == 20000
    assert g02["total_grams"] == 35000

    # 每片配重片仍恰好登记一次，归属守恒
    placements = {l["piece_id"]: "G-01" for l in g01["loads"]}
    placements.update({l["piece_id"]: "G-02" for l in g02["loads"]})
    assert placements == {"CW-A1": "G-01", "CW-A2": "G-02", "CW-B1": "G-02"}


def test_concurrent_opposite_transfers_deadlock_free_repeated(base_url):
    """重复多轮相反方向并发转移：固定顺序加锁，任何一轮都不得死锁或超限。

    X 在 G-01（8000 克）、Y 在 G-02（9000 克），每轮两笔相反方向转移
    并发提交；容量均允许时两片同时互换，下一轮方向再反过来。
    """
    load_piece(base_url, "G-01", "CW-ROT-X", 8000)
    load_piece(base_url, "G-02", "CW-ROT-Y", 9000)

    def locate(piece_id):
        for bid in ("G-01", "G-02"):
            detail = batten_state(base_url, bid)
            for item in detail["loads"]:
                if item["piece_id"] == piece_id:
                    return bid, item["load_id"]
        raise AssertionError(f"找不到配重片 {piece_id}")

    for _ in range(5):
        x_batten, id_x = locate("CW-ROT-X")
        y_batten, id_y = locate("CW-ROT-Y")
        x_target = "G-02" if x_batten == "G-01" else "G-01"
        y_target = "G-02" if y_batten == "G-01" else "G-01"
        barrier = threading.Barrier(2)

        def submit(call):
            barrier.wait(timeout=10)
            return call()

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    submit,
                    [
                        lambda: transfer(base_url, x_batten, id_x, x_target),
                        lambda: transfer(base_url, y_batten, id_y, y_target),
                    ],
                )
            )
        # 死锁会被 PostgreSQL 以 40P01 中止（500）；这里每笔都必须正常裁决
        assert all(r.status_code in (200, 409) for r in responses), [
            r.status_code for r in responses
        ]
        for bid in ("G-01", "G-02"):
            state = batten_state(base_url, bid)
            assert state["total_grams"] <= state["capacity_grams"]
            assert state["remaining_grams"] >= 0

    # 五轮互换后两片依旧各登记一次，总量守恒
    final = {bid: batten_state(base_url, bid) for bid in ("G-01", "G-02")}
    pieces = {l["piece_id"] for d in final.values() for l in d["loads"]}
    assert pieces == {"CW-ROT-X", "CW-ROT-Y"}
    assert sum(d["total_grams"] for d in final.values()) == 17000
