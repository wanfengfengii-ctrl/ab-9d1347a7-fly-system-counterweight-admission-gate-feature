"""标称重量修正：真实 HTTP + 真实 PostgreSQL 端到端验收，不使用任何假接口。"""

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


def correct_weight(base_url, batten_id, load_id, weight_grams):
    return httpx.post(
        f"{base_url}/api/battens/{batten_id}/loads/{load_id}/correct-weight",
        json={"weight_grams": weight_grams},
        timeout=30,
    )


def transfer(base_url, source_id, load_id, target_id):
    return httpx.post(
        f"{base_url}/api/battens/{source_id}/loads/{load_id}/transfer",
        json={"target_batten_id": target_id},
        timeout=30,
    )


def _load_id(detail, piece_id):
    return next(l["load_id"] for l in detail["loads"] if l["piece_id"] == piece_id)


def test_reducing_weight_recalculates_totals_and_preserves_identity(base_url):
    """减重：总重 / 余量按新旧重量差重算，标识、记录编号与登记时间保留。"""
    assert load_piece(base_url, "G-01", "CW-DOWN", 20000).status_code == 201
    before = batten_state(base_url, "G-01")
    load_before = next(l for l in before["loads"] if l["piece_id"] == "CW-DOWN")

    resp = correct_weight(base_url, "G-01", load_before["load_id"], 12000)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["batten_id"] == "G-01"
    assert body["total_grams"] == 12000
    assert body["remaining_grams"] == 18000
    assert body["load"]["load_id"] == load_before["load_id"]
    assert body["load"]["piece_id"] == "CW-DOWN"
    assert body["load"]["weight_grams"] == 12000
    assert body["load"]["previous_weight_grams"] == 20000
    assert "20000 克修正为 12000 克" in body["message"]

    # 不是删除重登：同一笔记录，配重标识与最初登记时间原样保留，明细仍只有一笔
    after = batten_state(base_url, "G-01")
    assert after["total_grams"] == 12000
    assert after["remaining_grams"] == 18000
    assert len(after["loads"]) == 1
    load_after = after["loads"][0]
    assert load_after["load_id"] == load_before["load_id"]
    assert load_after["piece_id"] == "CW-DOWN"
    assert load_after["weight_grams"] == 12000
    assert load_after["created_at"] == load_before["created_at"]


def test_increasing_weight_up_to_exact_capacity_is_allowed(base_url):
    """增重至满载：修正后恰好达到核定值必须允许写入。"""
    assert load_piece(base_url, "G-01", "CW-UP-A", 20000).status_code == 201
    assert load_piece(base_url, "G-01", "CW-UP-B", 5000).status_code == 201
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-UP-A")

    # 当前总重 25000：CW-UP-A 由 20000 增至 25000，合计恰好 30000
    resp = correct_weight(base_url, "G-01", load_id, 25000)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["total_grams"] == 30000
    assert body["remaining_grams"] == 0

    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 30000
    assert state["remaining_grams"] == 0
    weights = {l["piece_id"]: l["weight_grams"] for l in state["loads"]}
    assert weights == {"CW-UP-A": 25000, "CW-UP-B": 5000}


def test_over_capacity_correction_is_rejected_and_original_weight_kept(base_url):
    """增重导致超载被拒绝：数据库中的原重量保持不变。"""
    # 当前总重 25000（余 5000）：把 5000 克的 CW-OV-A 修正为 10001 克，
    # 新重量本身在单片范围内（100～25000），但合计 20000+10001=30001 超载
    assert load_piece(base_url, "G-01", "CW-OV-A", 5000).status_code == 201
    assert load_piece(base_url, "G-01", "CW-OV-B", 20000).status_code == 201
    before = batten_state(base_url, "G-01")
    load_id = _load_id(before, "CW-OV-A")

    resp = correct_weight(base_url, "G-01", load_id, 10001)
    assert resp.status_code == 409
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "OVER_CAPACITY"
    assert "超出核定" in body["message"]

    after = batten_state(base_url, "G-01")
    # 总重、余量与每片重量都回到修正前
    assert after["total_grams"] == 25000
    assert after["remaining_grams"] == 5000
    weights = {l["piece_id"]: l["weight_grams"] for l in after["loads"]}
    assert weights == {"CW-OV-A": 5000, "CW-OV-B": 20000}


def test_correction_after_transfer_is_rejected_as_position_changed(base_url):
    """记录已被其他终端转移：修正请求明确提示当前位置已变化，原重量不变。"""
    assert load_piece(base_url, "G-01", "CW-MOVED", 20000).status_code == 201
    before = batten_state(base_url, "G-01")
    load_before = next(l for l in before["loads"] if l["piece_id"] == "CW-MOVED")
    load_id = load_before["load_id"]

    # 另一终端先把片子转移到 G-02
    assert transfer(base_url, "G-01", load_id, "G-02").status_code == 200

    # 旧界面仍按 G-01 路径提交修正：必须报当前位置已变化
    resp = correct_weight(base_url, "G-01", load_id, 12000)
    assert resp.status_code == 409
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "POSITION_CHANGED"
    assert "当前位置已变化" in body["message"]

    # 片子留在 G-02，重量仍是修正前的 20000
    assert batten_state(base_url, "G-01")["loads"] == []
    g02 = batten_state(base_url, "G-02")
    assert len(g02["loads"]) == 1
    assert g02["loads"][0]["load_id"] == load_id
    assert g02["loads"][0]["weight_grams"] == 20000
    assert g02["loads"][0]["created_at"] == load_before["created_at"]


def test_correction_then_transfer_moves_the_corrected_weight(base_url):
    """先修正后转移：转移裁决与目标容量使用的是修正后的新重量。"""
    assert load_piece(base_url, "G-01", "CW-SEQ", 20000).status_code == 201
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-SEQ")

    assert correct_weight(base_url, "G-01", load_id, 9000).status_code == 200
    moved = transfer(base_url, "G-01", load_id, "G-02")
    assert moved.status_code == 200
    body = moved.json()
    assert body["weight_grams"] == 9000
    assert body["source"]["total_grams"] == 0
    assert body["target"]["total_grams"] == 9000
    assert body["target"]["remaining_grams"] == 41000

    g02 = batten_state(base_url, "G-02")
    assert g02["total_grams"] == 9000
    assert g02["loads"][0]["weight_grams"] == 9000


def test_concurrent_correction_and_transfer_serialize_without_over_capacity(base_url):
    """修正与转移并发：同一把吊杆行锁串行裁决，归属唯一、容量不超限、不死锁。

    G-01 空杆上挂 CW-RACE 20000 克，两笔请求同时发出：
      T1: CW-RACE 由 20000 克修正为 5000 克（路径 G-01）
      T2: CW-RACE 从 G-01 转移到 G-02
    两种合法串行次序：
      转移先到：转移成功（按 20000 克裁决），修正随后发现归属已变 → POSITION_CHANGED，
                片子以 20000 克留在 G-02；
      修正先到：修正成功，转移随后按 5000 克裁决并搬走，片子以 5000 克留在 G-02。
    """
    assert load_piece(base_url, "G-01", "CW-RACE", 20000).status_code == 201
    before = batten_state(base_url, "G-01")
    load_before = before["loads"][0]
    load_id = load_before["load_id"]

    barrier = threading.Barrier(2)

    def submit(call):
        barrier.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                submit,
                [
                    lambda: correct_weight(base_url, "G-01", load_id, 5000),
                    lambda: transfer(base_url, "G-01", load_id, "G-02"),
                ],
            )
        )

    correction_resp, transfer_resp = responses
    # 转移必然成功（两种次序下 G-02 都放得下），且无死锁 / 500
    assert transfer_resp.status_code == 200, transfer_resp.text

    if correction_resp.status_code == 200:
        # 修正先提交：最终片子以修正后的 5000 克挂在 G-02
        assert correction_resp.json()["accepted"] is True
        expected_weight = 5000
    else:
        # 转移先提交：修正读到新归属并拒绝
        assert correction_resp.status_code == 409, correction_resp.text
        assert correction_resp.json()["reason"] == "POSITION_CHANGED"
        expected_weight = 20000

    g01 = batten_state(base_url, "G-01")
    g02 = batten_state(base_url, "G-02")
    # 归属唯一：G-01 清空，G-02 恰好挂这一笔
    assert g01["loads"] == []
    assert g01["total_grams"] == 0
    assert len(g02["loads"]) == 1
    final_load = g02["loads"][0]
    assert final_load["load_id"] == load_id
    assert final_load["piece_id"] == "CW-RACE"
    assert final_load["weight_grams"] == expected_weight
    # 无论哪种次序，容量都不超限，登记时间保留
    assert g02["total_grams"] == expected_weight
    assert g02["remaining_grams"] == 50000 - expected_weight
    assert g02["remaining_grams"] >= 0
    assert final_load["created_at"] == load_before["created_at"]


def test_out_of_range_weight_is_rejected_and_unchanged(base_url):
    assert load_piece(base_url, "G-01", "CW-BOUNDS", 10000).status_code == 201
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-BOUNDS")

    for bad_weight in (99, 0, 25001, -100):
        resp = correct_weight(base_url, "G-01", load_id, bad_weight)
        assert resp.status_code == 422
        assert resp.json()["reason"] == "INVALID_WEIGHT"

    state = batten_state(base_url, "G-01")
    assert state["loads"][0]["weight_grams"] == 10000
    assert state["total_grams"] == 10000


def test_non_integer_weight_is_rejected_without_change(base_url):
    assert load_piece(base_url, "G-01", "CW-TYPE", 10000).status_code == 201
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-TYPE")

    for raw in ('{"weight_grams":"12000"}', '{"weight_grams":12000.5}'):
        resp = httpx.post(
            f"{base_url}/api/battens/G-01/loads/{load_id}/correct-weight",
            content=raw,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["reason"] == "INVALID_INPUT"

    state = batten_state(base_url, "G-01")
    assert state["loads"][0]["weight_grams"] == 10000


def test_unknown_load_and_batten_are_rejected_distinctly(base_url):
    resp = correct_weight(base_url, "G-01", 999999, 5000)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "LOAD_NOT_FOUND"

    resp = correct_weight(base_url, "G-99", 1, 5000)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "BATTEN_NOT_FOUND"


def test_existing_load_query_transfer_reset_contracts_still_work(base_url):
    """新增修正能力不破坏既有装载 / 查询 / 转移 / 重置接口与字段。"""
    assert load_piece(base_url, "G-01", "CW-KEEP", 8000).status_code == 201
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-KEEP")
    assert correct_weight(base_url, "G-01", load_id, 9000).status_code == 200

    # 明细查询字段保持兼容
    detail = batten_state(base_url, "G-01")
    (item,) = detail["loads"]
    assert set(item) == {"load_id", "piece_id", "weight_grams", "created_at"}

    # 转移后重置：reset 仍清空全部记录
    assert transfer(base_url, "G-01", load_id, "G-02").status_code == 200
    reset = httpx.post(f"{base_url}/api/reset", timeout=10)
    assert reset.status_code == 200
    reset_body = reset.json()
    totals = {b["batten_id"]: b["total_grams"] for b in reset_body["battens"]}
    assert totals == {"G-01": 0, "G-02": 0}
