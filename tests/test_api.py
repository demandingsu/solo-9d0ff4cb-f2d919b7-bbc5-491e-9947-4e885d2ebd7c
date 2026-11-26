"""提词器 API 业务规则测试。

每个测试使用独立的临时 SQLite 文件，并通过重建 app 连接模拟"重启"。
"""

from __future__ import annotations

import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "prompter.db")
    monkeypatch.setenv("PROMPTER_DB", db_path)

    import app.main as main

    importlib.reload(main)
    main.init_db()
    with TestClient(main.app) as c:
        c.db_path = db_path  # type: ignore[attr-defined]
        c.main = main  # type: ignore[attr-defined]
        yield c


def script(lines):
    return {"lines": [{"line_no": i, "text": t} for i, t in enumerate(lines, start=1)]}


def fetch(client, pid, last):
    return client.post(f"/api/prompters/{pid}/lines", json={"last_line_no": last})


# --------------------------------------------------------------------------- #
def test_health_and_empty_state(client):
    assert client.get("/health").json() == {"status": "ok"}
    info = client.get("/api/script").json()
    assert info == {"total_lines": 0, "version": 0}
    progress = client.get("/api/progress").json()
    assert progress == [
        {"prompter_id": 1, "last_line_no": 0},
        {"prompter_id": 2, "last_line_no": 0},
    ]


def test_initial_submit_and_sequential_fetch(client):
    r = client.post("/api/script", json=script(["开场白", "第二句", "第三句"]))
    assert r.status_code == 200
    assert r.json()["total_lines"] == 3

    # 开场传 0，两台提词器各自独立领词。
    r1 = fetch(client, 1, 0)
    assert r1.status_code == 200
    assert r1.json() == {
        "status": "ok",
        "prompter_id": 1,
        "last_line_no": 1,
        "line": {"line_no": 1, "text": "开场白"},
    }
    r2 = fetch(client, 2, 0)
    assert r2.json()["line"]["line_no"] == 1

    r1 = fetch(client, 1, 1)
    assert r1.json()["last_line_no"] == 2
    assert r1.json()["line"]["text"] == "第二句"


def test_resend_same_request_is_idempotent(client):
    client.post("/api/script", json=script(["甲", "乙", "丙"]))
    first = fetch(client, 1, 0).json()
    # 网络重试：同样的 last_line_no=0 再发一次。
    again = fetch(client, 1, 0).json()
    assert again == first
    # 进度没有被重复推进。
    assert client.get("/api/prompters/1/progress").json()["last_line_no"] == 1
    third = fetch(client, 1, 0).json()
    assert third == first


def test_gap_request_rejected(client):
    client.post("/api/script", json=script(["甲", "乙", "丙"]))
    # 凭空跳号：还没领过第 1 行就声称 last_line_no=2。
    r = fetch(client, 1, 2)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "gap"
    assert r.json()["detail"]["current_last_line_no"] == 0
    # 被拒绝后进度不变，仍可正常领第 1 行。
    ok = fetch(client, 1, 0)
    assert ok.json()["line"]["line_no"] == 1
    # 已领到第 1 行后，跳号到 3 同样拒绝。
    r = fetch(client, 1, 3)
    assert r.status_code == 409
    assert r.json()["detail"]["submitted_last_line_no"] == 3


def test_end_of_script_no_line_then_continue(client):
    client.post("/api/script", json=script(["仅一句"]))
    fetch(client, 1, 0)
    # 到稿尾：先报暂无，进度不动。
    tail = fetch(client, 1, 1)
    assert tail.status_code == 200
    assert tail.json()["status"] == "no_line"
    assert tail.json()["line"] is None
    assert tail.json()["last_line_no"] == 1
    # 再请求仍是暂无。
    assert fetch(client, 1, 1).json()["status"] == "no_line"

    # 导演续稿（前缀完全一致），换稿成功。
    r = client.post("/api/script", json=script(["仅一句", "续写第二句", "第三句"]))
    assert r.status_code == 200
    # 用原来的请求（last_line_no=1）即可领到下一行。
    resumed = fetch(client, 1, 1)
    assert resumed.json() == {
        "status": "ok",
        "prompter_id": 1,
        "last_line_no": 2,
        "line": {"line_no": 2, "text": "续写第二句"},
    }


def test_proofreading_keeps_serving_old_script(client):
    client.post("/api/script", json=script(["原1", "原2", "原3"]))
    fetch(client, 1, 0)  # 提词器 1 已领第 1 行
    # 导演提交与已领行冲突的新稿：首处不符即整份退回。
    r = client.post("/api/script", json=script(["改了的第一行", "新2"]))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "line_mismatch"
    assert detail["line_no"] == 1
    assert detail["existing_text"] == "原1"
    assert detail["submitted_text"] == "改了的第一行"
    # 原稿与进度都不变，继续用原稿供词。
    assert client.get("/api/script").json()["total_lines"] == 3
    nxt = fetch(client, 1, 1)
    assert nxt.json()["line"] == {"line_no": 2, "text": "原2"}

    # 首处不符定位：第 1 行一致、第 2 行不符，应报行号 2。
    r = client.post("/api/script", json=script(["原1", "改动第二行", "原3", "新增4"]))
    assert r.status_code == 409
    assert r.json()["detail"]["line_no"] == 2

    # 缺少已领行也拒绝：先把 p1 领到第 2 行，再提交只有 1 行的新稿。
    fetch(client, 1, 2)  # p1 已领至第 2 行
    r = client.post("/api/script", json=script(["原1"]))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "missing_taken_line"
    assert r.json()["detail"]["line_no"] == 2
    # 退回后原稿仍是 3 行。
    assert client.get("/api/script").json()["total_lines"] == 3


def test_interleaved_switch_preserves_taken_lines(client):
    """两台提词器交错领词、中途换稿：任一台领过的行都保留原行号与文字。"""
    client.post("/api/script", json=script(["A1", "A2", "A3"]))
    fetch(client, 1, 0)   # p1 -> 1
    fetch(client, 2, 0)   # p2 -> 1
    fetch(client, 1, 1)   # p1 -> 2
    fetch(client, 2, 1)   # p2 -> 2
    fetch(client, 1, 2)   # p1 -> 3（p1 比 p2 多领一行）

    # 新稿保留 1..3 的文字，追加 4、5 行 —— 两台已领行全部兼容。
    r = client.post(
        "/api/script",
        json=script(["A1", "A2", "A3", "B4", "B5"]),
    )
    assert r.status_code == 200
    assert r.json()["version"] == 2

    # p2 当前进度为 2。模拟它重发"已成功过的"旧请求 last_line_no=1，
    # 服务端幂等返回它上次领到的第 2 行，进度不变。
    again = fetch(client, 2, 1)
    assert again.json()["line"] == {"line_no": 2, "text": "A2"}
    assert again.json()["last_line_no"] == 2
    # 再重发一次结果完全一致（可安全重试）。
    assert fetch(client, 2, 1).json() == again.json()
    # p2 带着最新进度 2 正常续领，行号在原稿之后延续，拿到新稿第 3 行。
    nxt = fetch(client, 2, 2)
    assert nxt.json()["line"] == {"line_no": 3, "text": "A3"}
    assert nxt.json()["last_line_no"] == 3
    nxt = fetch(client, 2, 3)
    assert nxt.json()["line"] == {"line_no": 4, "text": "B4"}

    # p1（进度 3）继续领新稿追加的行。
    nxt = fetch(client, 1, 3)
    assert nxt.json()["line"] == {"line_no": 4, "text": "B4"}

    # 新稿若改动 p1 已领的第 3 行，即使 p2 没领到也必须拒绝（首处不符报行号 3）。
    r = client.post("/api/script", json=script(["A1", "A2", "被篡改的3", "B4"]))
    assert r.status_code == 409
    assert r.json()["detail"]["line_no"] == 3
    # 被退回后当前稿仍是 5 行的版本。
    assert client.get("/api/script").json()["total_lines"] == 5


def test_switch_then_prompter_continues(client):
    """换稿后提词器带着最新进度继续领词，行号在原稿之后延续。"""
    client.post("/api/script", json=script(["A1", "A2"]))
    fetch(client, 1, 0)
    fetch(client, 1, 1)  # p1 -> 2
    r = client.post("/api/script", json=script(["A1", "A2", "A3", "A4"]))
    assert r.status_code == 200
    r = fetch(client, 1, 2)
    assert r.json()["line"] == {"line_no": 3, "text": "A3"}
    r = fetch(client, 1, 3)
    assert r.json()["line"] == {"line_no": 4, "text": "A4"}
    assert fetch(client, 1, 4).json()["status"] == "no_line"


def test_structure_validation(client):
    # 行号重复。
    r = client.post(
        "/api/script",
        json={"lines": [{"line_no": 1, "text": "x"}, {"line_no": 1, "text": "y"}]},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "duplicate_line_no"
    # 不从 1 开始。
    r = client.post("/api/script", json={"lines": [{"line_no": 2, "text": "x"}]})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "non_consecutive_line_no"
    assert r.json()["detail"]["line_no"] == 2
    # 缺号：1, 3。
    r = client.post(
        "/api/script",
        json={"lines": [{"line_no": 1, "text": "x"}, {"line_no": 3, "text": "y"}]},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["line_no"] == 3
    # 原稿仍为空，结构错误没有污染数据。
    assert client.get("/api/script").json()["total_lines"] == 0


def test_invalid_prompter_id(client):
    assert fetch(client, 3, 0).status_code == 404
    r = client.post("/api/prompters/1/lines", json={"last_line_no": -1})
    assert r.status_code == 422


def test_empty_script_and_blank_lines(client):
    # 空稿允许提交：结构合法，此时领词一律 no_line。
    r = client.post("/api/script", json={"lines": []})
    assert r.status_code == 200
    assert r.json()["total_lines"] == 0
    r = fetch(client, 1, 0)
    assert r.json()["status"] == "no_line"
    assert r.json()["last_line_no"] == 0
    # 空字符串文字也是合法行。
    r = client.post("/api/script", json=script(["", "第二行"]))
    assert r.status_code == 200
    r = fetch(client, 1, 0)
    assert r.json()["line"] == {"line_no": 1, "text": ""}


def test_resend_any_historical_value_returns_current_line(client):
    client.post("/api/script", json=script(["甲", "乙", "丙", "丁"]))
    for n in range(4):
        fetch(client, 1, n)  # 领到第 4 行
    # 任意落后的历史值都幂等返回"当前已领到的那一行"（第 4 行）。
    for stale in (0, 1, 2, 3):
        r = fetch(client, 1, stale)
        assert r.status_code == 200
        assert r.json()["line"] == {"line_no": 4, "text": "丁"}
        assert r.json()["last_line_no"] == 4
    assert client.get("/api/prompters/1/progress").json()["last_line_no"] == 4


def test_switch_does_not_touch_other_prompter(client):
    client.post("/api/script", json=script(["A1", "A2", "A3"]))
    fetch(client, 1, 0)
    fetch(client, 1, 1)  # p1 -> 2
    fetch(client, 2, 0)  # p2 -> 1
    client.post("/api/script", json=script(["A1", "A2", "A3", "A4"]))
    # 两台进度互不影响。
    progress = {p["prompter_id"]: p["last_line_no"] for p in client.get("/api/progress").json()}
    assert progress == {1: 2, 2: 1}
    # p2 续领。
    r = fetch(client, 2, 1)
    assert r.json()["line"] == {"line_no": 2, "text": "A2"}


def test_restart_persists_script_and_progress(client):
    """模拟进程重启：数据库文件不变，重新初始化后沿用稿件与各台进度。"""
    client.post("/api/script", json=script(["持久1", "持久2", "持久3"]))
    fetch(client, 1, 0)
    fetch(client, 1, 1)  # p1 -> 2
    fetch(client, 2, 0)  # p2 -> 1

    main = client.main  # type: ignore[attr-defined]
    db_path = client.db_path  # type: ignore[attr-defined]
    del client

    importlib.reload(main)
    assert main.DB_PATH == db_path
    main.init_db()
    with TestClient(main.app) as restarted:
        # 稿件还在。
        assert restarted.get("/api/script").json()["total_lines"] == 3
        # 两台进度沿用。
        progress = {p["prompter_id"]: p["last_line_no"] for p in restarted.get("/api/progress").json()}
        assert progress == {1: 2, 2: 1}
        # p1 用重启前的最后行号继续领词。
        r = restarted.post("/api/prompters/1/lines", json={"last_line_no": 2})
        assert r.json()["line"] == {"line_no": 3, "text": "持久3"}
        # p2 重发旧请求，幂等返回原来那一行。
        r = restarted.post("/api/prompters/2/lines", json={"last_line_no": 0})
        assert r.json()["line"] == {"line_no": 1, "text": "持久1"}
        assert r.json()["last_line_no"] == 1
