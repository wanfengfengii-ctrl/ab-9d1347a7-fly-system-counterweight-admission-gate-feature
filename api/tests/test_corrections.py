"""装台复核修正重量：真实 HTTP + 真实 PostgreSQL 端到端验收，不使用任何假接口。"""

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


def correct(base_url, batten_id, load_id, weight_grams):
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


def test_reduce_weight_recalculates_totals_and_preserves_identity(base_url):
    """减重：直接改明细重量，不删除、不重新登记，标识与最初登记时间保留。"""
    assert load_piece(base_url, "G-01", "CW-DOWN", 20000).status_code == 201
    before = batten_state(base_url, "G-01")
    load_before = next(l for l in before["loads"] if l["piece_id"] == "CW-DOWN")

    resp = correct(base_url, "G-01", load_before["load_id"], 12000)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["batten_id"] == "G-01"
    assert body["load_id"] == load_before["load_id"]
    assert body["piece_id"] == "CW-DOWN"
    assert body["previous_weight_grams"] == 20000
    assert body["weight_grams"] == 12000
    assert body["capacity_grams"] == 30000
    # 以新旧重量差重新核算：总重与余量同步刷新
    assert body["total_grams"] == 12000
    assert body["remaining_grams"] == 18000

    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 12000
    assert state["remaining_grams"] == 18000
    assert len(state["loads"]) == 1
    load_after = state["loads"][0]
    # 同一条记录：load_id / piece_id / created_at 全部保留，只有重量变化
    assert load_after["load_id"] == load_before["load_id"]
    assert load_after["piece_id"] == "CW-DOWN"
    assert load_after["weight_grams"] == 12000
    assert load_after["created_at"] == load_before["created_at"]


def test_increase_weight_up_to_exact_capacity_is_allowed(base_url):
    """增重至满载：修正后合计恰好等于核定值允许写入。"""
    assert load_piece(base_url, "G-01", "CW-UP-A", 10000).status_code == 201
    assert load_piece(base_url, "G-01", "CW-UP-B", 10000).status_code == 201
    target = _load_id(batten_state(base_url, "G-01"), "CW-UP-B")

    # 10000 + 20000 = 30000，恰好达到核定值
    resp = correct(base_url, "G-01", target, 20000)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["total_grams"] == 30000
    assert body["remaining_grams"] == 0

    state = batten_state(base_url, "G-01")
    weights = {l["piece_id"]: l["weight_grams"] for l in state["loads"]}
    assert weights == {"CW-UP-A": 10000, "CW-UP-B": 20000}
    assert state["total_grams"] == 30000
    assert state["remaining_grams"] == 0


def test_increase_beyond_capacity_is_rejected_and_old_weight_kept(base_url):
    """超载拒绝：修正后合计超出核定值则拒绝，数据库中的原重量保持不变。"""
    assert load_piece(base_url, "G-01", "CW-OV-A", 10000).status_code == 201
    assert load_piece(base_url, "G-01", "CW-OV-B", 10000).status_code == 201
    before = batten_state(base_url, "G-01")
    target = _load_id(before, "CW-OV-B")
    created_before = next(l for l in before["loads"] if l["piece_id"] == "CW-OV-B")[
        "created_at"
    ]

    # 10000 + 20001 = 30001 > 30000
    resp = correct(base_url, "G-01", target, 20001)
    assert resp.status_code == 409
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "OVER_CAPACITY"
    assert "超出核定" in body["message"]

    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 20000
    assert state["remaining_grams"] == 10000
    load_after = next(l for l in state["loads"] if l["piece_id"] == "CW-OV-B")
    # 原重量与最初登记时间保持不变，记录未被删除或重建
    assert load_after["weight_grams"] == 10000
    assert load_after["created_at"] == created_before


def test_new_weight_out_of_range_is_rejected_and_unchanged(base_url):
    """新重量越界沿用单片范围 100～25000：拒绝且原重量不变。"""
    assert load_piece(base_url, "G-01", "CW-RANGE", 1000).status_code == 201
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-RANGE")

    for bad_weight in (0, 99, 25001, -500):
        resp = correct(base_url, "G-01", load_id, bad_weight)
        assert resp.status_code == 422
        assert resp.json()["reason"] == "INVALID_WEIGHT"

    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 1000
    assert state["loads"][0]["weight_grams"] == 1000


def test_non_integer_new_weight_is_rejected(base_url):
    """字符串、小数、布尔等伪装的新重量必须明确拒绝且不保存。"""
    assert load_piece(base_url, "G-01", "CW-TYPE", 1000).status_code == 201
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-TYPE")

    # 手工构造原始 JSON 报文，避免客户端把 100.0 序列化成 100
    for raw in (
        '{"weight_grams":"100"}',
        '{"weight_grams":100.0}',
        '{"weight_grams":true}',
        '{}',
    ):
        resp = httpx.post(
            f"{base_url}/api/battens/G-01/loads/{load_id}/correct-weight",
            content=raw,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert resp.status_code == 422, f"载荷 {raw} 未被拒绝: {resp.text}"
        body = resp.json()
        assert body["accepted"] is False
        assert body["reason"] == "INVALID_INPUT"

    state = batten_state(base_url, "G-01")
    assert state["loads"][0]["weight_grams"] == 1000


def test_correct_unknown_load_or_batten_is_rejected(base_url):
    assert load_piece(base_url, "G-01", "CW-EX", 1000).status_code == 201

    # 记录编号从未存在：明确 404，不与"已被移走"混淆
    resp = correct(base_url, "G-01", 999999, 2000)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "LOAD_NOT_FOUND"

    resp = correct(base_url, "G-99", 1, 2000)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "BATTEN_NOT_FOUND"


def test_correct_after_transfer_reports_position_changed(base_url):
    """记录已被其他终端转移：修正返回当前位置已变化，原重量保持不变。"""
    assert load_piece(base_url, "G-01", "CW-GONE", 5000).status_code == 201
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-GONE")

    # 另一终端先把片子转移到 G-02
    assert transfer(base_url, "G-01", load_id, "G-02").status_code == 200

    # 旧界面仍以为它在 G-01，提交修正：当前位置已变化
    resp = correct(base_url, "G-01", load_id, 8000)
    assert resp.status_code == 409
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "POSITION_CHANGED"
    assert "当前位置已变化" in body["message"]

    # 重量未被修改，片子留在 G-02
    assert batten_state(base_url, "G-01")["loads"] == []
    dst = batten_state(base_url, "G-02")
    assert len(dst["loads"]) == 1
    assert dst["loads"][0]["piece_id"] == "CW-GONE"
    assert dst["loads"][0]["weight_grams"] == 5000


def test_concurrent_correction_and_transfer_keeps_ownership_and_capacity_consistent(
    base_url,
):
    """修正与转移并发：固定先锁路径吊杆串行裁决，归属与容量始终一致、不死锁。

    G-01 挂 P 共 20000 克，G-02 为空（可容纳任意结果）：
      T1: 修正 P 20000 → 22000（仍在 G-01）
      T2: 转移 P G-01 → G-02
    两者都先抢 G-01 行锁：
      - 修正先到：修正成功（P=22000 留在 G-01），随后转移把 22000 的 P 搬到
        G-02，最终 P 在 G-02、重 22000；
      - 转移先到：P 以 20000 搬到 G-02，随后修正发现归属已变 → POSITION_CHANGED，
        最终 P 在 G-02、重 20000。
    """
    assert load_piece(base_url, "G-01", "CW-RACE-P", 20000).status_code == 201
    before = batten_state(base_url, "G-01")
    load_before = next(l for l in before["loads"] if l["piece_id"] == "CW-RACE-P")
    load_id = load_before["load_id"]
    created_at = load_before["created_at"]

    barrier = threading.Barrier(2)

    def submit(call):
        barrier.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                submit,
                [
                    lambda: correct(base_url, "G-01", load_id, 22000),
                    lambda: transfer(base_url, "G-01", load_id, "G-02"),
                ],
            )
        )

    correct_resp, transfer_resp = responses
    assert correct_resp.status_code in (200, 409)
    assert transfer_resp.status_code == 200

    g01 = batten_state(base_url, "G-01")
    g02 = batten_state(base_url, "G-02")

    # 任何裁决顺序下 P 都恰好存在一次、位于 G-02
    assert g01["loads"] == []
    assert g01["total_grams"] == 0
    assert len(g02["loads"]) == 1
    moved = g02["loads"][0]
    assert moved["load_id"] == load_id
    assert moved["piece_id"] == "CW-RACE-P"
    assert moved["created_at"] == created_at

    if correct_resp.status_code == 200:
        # 修正在转移之前落盘：重量为新值
        assert correct_resp.json()["accepted"] is True
        assert moved["weight_grams"] == 22000
    else:
        # 转移先行：修正如实报告位置变化，重量保持原值
        assert correct_resp.json()["reason"] == "POSITION_CHANGED"
        assert moved["weight_grams"] == 20000

    # 容量按最终归属与重量核算：两杆均不超限，总重 / 余量自洽
    assert g02["total_grams"] == moved["weight_grams"]
    assert g02["remaining_grams"] == 50000 - moved["weight_grams"]
    assert g01["total_grams"] <= g01["capacity_grams"]
    assert g02["total_grams"] <= g02["capacity_grams"]
    assert g01["remaining_grams"] >= 0
    assert g02["remaining_grams"] >= 0
