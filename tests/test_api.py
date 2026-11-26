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
        "stop": None,
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
        "stop": None,
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


# =========================================================================== #
# 换景停点（舞监放行）
# =========================================================================== #
def create_stop(client, cue):
    return client.post("/api/stops", json={"cue_text": cue})


def release(client, stop_id):
    return client.post(f"/api/stops/{stop_id}/release")


def pending_stop(client):
    return client.get("/api/stops/pending")


def test_create_stop_validation(client):
    client.post("/api/script", json=script(["甲", "换景", "丙", "换景"]))
    # 口令 0 行匹配。
    r = create_stop(client, "不存在的口令")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "cue_not_unique"
    assert r.json()["detail"]["match_count"] == 0
    # 口令多行匹配 -> 拒绝。
    r = create_stop(client, "换景")
    assert r.status_code == 409
    assert r.json()["detail"]["match_count"] == 2

    # 唯一口令可以设停点。
    r = create_stop(client, "丙")
    assert r.status_code == 201
    body = r.json()
    assert body["stop_id"] >= 1
    assert body["cue_text"] == "丙"
    assert body["line_no"] == 3
    assert body["status"] == "pending"
    assert body["arrived_prompter_ids"] == []

    # 同一时间只允许一个待放行停点。
    r = create_stop(client, "甲")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "pending_stop_exists"

    # 未到齐不能放行，停点保持原状。
    r = release(client, body["stop_id"])
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "not_all_arrived"
    assert pending_stop(client).json()["stop_id"] == body["stop_id"]


def test_stop_line_already_taken_rejected(client):
    client.post("/api/script", json=script(["甲", "乙", "丙"]))
    fetch(client, 1, 0)  # p1 -> 1
    fetch(client, 2, 0)  # p2 -> 1
    # 第 1 行两台都领过了，不能再设停点。
    r = create_stop(client, "甲")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "line_already_taken"
    assert r.json()["detail"]["line_no"] == 1
    # 只要有一台领过该行也不行（p1 领到 2，p2 仍在 1）。
    fetch(client, 1, 1)  # p1 -> 2
    r = create_stop(client, "乙")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "line_already_taken"


def test_one_prompter_arrives_first_then_other_and_release(client):
    """场景一：一台先到 -> 等候且进度不动；另一台照常领到停点行；到齐放行后续领。"""
    client.post("/api/script", json=script(["第一句", "换景口令", "第三句", "第四句"]))
    stop = create_stop(client, "换景口令").json()
    stop_id = stop["stop_id"]
    assert stop["line_no"] == 2

    # 两台先各领第 1 行（停点行之前的行正常领）。
    assert fetch(client, 1, 0).json()["line"]["line_no"] == 1
    assert fetch(client, 2, 0).json()["line"]["line_no"] == 1

    # p1 先到：取下一行（第 2 行=停点行）-> waiting，进度不动。
    r = fetch(client, 1, 1)
    assert r.status_code == 200
    assert r.json()["status"] == "waiting"
    assert r.json()["line"] is None
    assert r.json()["last_line_no"] == 1
    assert r.json()["stop"]["stop_id"] == stop_id
    assert r.json()["stop"]["arrived_prompter_ids"] == [1]
    assert client.get("/api/prompters/1/progress").json()["last_line_no"] == 1

    # 先到的一台再取（哪怕换个请求参数）仍是等候，进度不动。
    r = fetch(client, 1, 1)
    assert r.json()["status"] == "waiting"
    assert r.json()["last_line_no"] == 1
    # 重发历史请求 last_line_no=0（< 当前进度 1）也因已到停点而等候。
    assert fetch(client, 1, 0).json()["status"] == "waiting"

    # 未到齐提前放行 -> 409，停点保持原状。
    r = release(client, stop_id)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "not_all_arrived"
    assert r.json()["detail"]["arrived_prompter_ids"] == [1]
    assert pending_stop(client).json()["status"] == "pending"

    # 编号不符的放行 -> 409，停点保持。
    r = release(client, stop_id + 999)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "stop_not_found"
    assert pending_stop(client).json()["stop_id"] == stop_id

    # p2 随后到：照常领到停点行（第 2 行），进度推进到 2。
    r = fetch(client, 2, 1)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["line"] == {"line_no": 2, "text": "换景口令"}
    assert r.json()["last_line_no"] == 2
    assert r.json()["stop"] is None
    # p2 已登记到台。
    assert pending_stop(client).json()["arrived_prompter_ids"] == [1, 2]

    # 到齐后但放行前，两台的后续请求仍等候（p2 已到，请求第 3 行也不放行）。
    assert fetch(client, 2, 2).json()["status"] == "waiting"
    assert client.get("/api/prompters/2/progress").json()["last_line_no"] == 2
    assert fetch(client, 1, 1).json()["status"] == "waiting"

    # 舞监凭本次停点编号放行。
    r = release(client, stop_id)
    assert r.status_code == 200
    assert r.json()["status"] == "released"
    assert r.json()["stop"]["status"] == "released"
    assert r.json()["stop"]["arrived_prompter_ids"] == [1, 2]
    assert pending_stop(client).json() is None

    # 放行后两台用原来的请求继续领词。
    r = fetch(client, 1, 1)  # p1 原请求：领到停点行第 2 行
    assert r.json()["status"] == "ok"
    assert r.json()["line"] == {"line_no": 2, "text": "换景口令"}
    assert r.json()["last_line_no"] == 2
    r = fetch(client, 2, 2)  # p2 原请求：领到第 3 行
    assert r.json()["line"] == {"line_no": 3, "text": "第三句"}
    assert r.json()["last_line_no"] == 3
    # 已放行停点不再阻挡后续领词。
    assert fetch(client, 1, 2).json()["line"]["line_no"] == 3
    # p1 尚未领第 4 行时，可在第 4 行设下一个停点。
    nxt = create_stop(client, "第四句")
    assert nxt.status_code == 201
    # 历史停点可查。
    ids = [s["stop_id"] for s in client.get("/api/stops").json()]
    assert stop_id in ids and nxt.json()["stop_id"] in ids
    # 新停点同样在第 4 行拦住 p1；重发原请求仍是等候，跳号请求仍按 gap 拒绝。
    assert fetch(client, 1, 3).json()["status"] == "waiting"
    assert client.post(
        "/api/prompters/1/lines", json={"last_line_no": 4}
    ).json()["detail"]["reason"] == "gap"
    # 另一台到齐放行后才能到稿尾。
    assert fetch(client, 2, 3).json()["line"]["line_no"] == 4
    release(client, nxt.json()["stop_id"])
    assert fetch(client, 1, 3).json()["line"]["line_no"] == 4
    assert fetch(client, 1, 4).json()["status"] == "no_line"


def test_stop_arrival_order_symmetric(client):
    """换一台先到同样成立：先到者等候，后到者领到停点行。"""
    client.post("/api/script", json=script(["甲", "停", "丙"]))
    stop_id = create_stop(client, "停").json()["stop_id"]
    fetch(client, 1, 0)
    fetch(client, 2, 0)
    # p2 先到。
    r = fetch(client, 2, 1)
    assert r.json()["status"] == "waiting"
    assert r.json()["stop"]["arrived_prompter_ids"] == [2]
    # p1 后到，领到停点行。
    r = fetch(client, 1, 1)
    assert r.json()["status"] == "ok"
    assert r.json()["line"]["line_no"] == 2
    assert pending_stop(client).json()["arrived_prompter_ids"] == [1, 2]
    # 到齐放行。
    assert release(client, stop_id).status_code == 200


def test_switch_script_moves_stop_when_nobody_arrived(client):
    """场景二（交错 A）：两台都未到时换稿，口令唯一且落在未领行 -> 停点随行移动。"""
    client.post("/api/script", json=script(["甲", "乙", "换景", "丁"]))
    stop = create_stop(client, "换景").json()
    assert stop["line_no"] == 3
    # 两台都只领到第 1 行，均未到停点。
    fetch(client, 1, 0)
    fetch(client, 2, 0)

    # 新稿：已领行（第 1 行）保持；口令"换景"移到第 4 行，且两台都没领过。
    r = client.post(
        "/api/script",
        json=script(["甲", "乙改", "丙改", "换景", "戊"]),
    )
    assert r.status_code == 200
    moved = pending_stop(client).json()
    assert moved["stop_id"] == stop["stop_id"]
    assert moved["line_no"] == 4
    assert moved["status"] == "pending"
    assert moved["arrived_prompter_ids"] == []

    # 新稿生效后领词按新行号在第 4 行停下。
    assert fetch(client, 1, 1).json()["line"]["line_no"] == 2
    r = fetch(client, 1, 2)
    assert r.json()["status"] == "ok" and r.json()["line"]["line_no"] == 3
    r = fetch(client, 1, 3)
    assert r.json()["status"] == "waiting"
    assert r.json()["stop"]["line_no"] == 4


def test_switch_script_rejects_when_cue_not_unique(client):
    client.post("/api/script", json=script(["甲", "换景", "丙"]))
    stop = create_stop(client, "换景").json()
    fetch(client, 1, 0)
    fetch(client, 2, 0)
    # 新稿口令出现两次 -> 整稿退回，停点保持原状。
    r = client.post("/api/script", json=script(["甲", "换景", "新丙", "换景"]))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "cue_not_unique"
    assert r.json()["detail"]["match_count"] == 2
    # 原稿与停点都不变。
    assert client.get("/api/script").json()["total_lines"] == 3
    assert pending_stop(client).json()["line_no"] == stop["line_no"]
    # 口令 0 次同样退回。
    r = client.post("/api/script", json=script(["甲", "没口令", "丙"]))
    assert r.status_code == 409
    assert r.json()["detail"]["match_count"] == 0
    assert pending_stop(client).json()["line_no"] == 2


def test_switch_script_fixes_line_once_one_arrived(client):
    """场景二（交错 B）：一台已到后换稿，停点固定原行号，口令移位即整稿退回。"""
    client.post("/api/script", json=script(["甲", "换景", "丙", "丁"]))
    stop = create_stop(client, "换景").json()
    stop_id = stop["stop_id"]
    fetch(client, 1, 0)
    fetch(client, 2, 0)
    # p1 先到停点行（第 2 行）。
    assert fetch(client, 1, 1).json()["status"] == "waiting"

    # 口令仍在原行号 2：即使其他未领行改动也允许，停点行号固定不变。
    r = client.post("/api/script", json=script(["甲", "换景", "丙改", "丁改", "新加5"]))
    assert r.status_code == 200
    kept = pending_stop(client).json()
    assert kept["stop_id"] == stop_id and kept["line_no"] == 2
    assert kept["arrived_prompter_ids"] == [1]
    # p1 仍处于等候。
    assert fetch(client, 1, 1).json()["status"] == "waiting"

    # 口令移到别的行号 -> 固定行号冲突，整稿退回。
    r = client.post("/api/script", json=script(["甲", "乙改", "换景", "丁改"]))
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "stop_line_fixed"
    assert r.json()["detail"]["line_no"] == 2
    # 停点与进度照旧。
    assert pending_stop(client).json()["line_no"] == 2
    assert client.get("/api/prompters/1/progress").json()["last_line_no"] == 1

    # p2 到达、放行后用原请求继续。
    assert fetch(client, 2, 1).json()["line"]["line_no"] == 2
    assert release(client, stop_id).status_code == 200
    assert fetch(client, 1, 1).json()["line"]["line_no"] == 2


def test_switch_script_cannot_move_onto_taken_line(client):
    """两台都未到时，停点只允许移到两台都没领过的行。

    已领行的行号+文字被换稿规则锁定，而口令在旧稿中唯一，因此把口令移到
    任一已领行必然与已领行文字冲突，换稿被整稿退回，停点与进度照旧。
    """
    # 停点设在第 4 行（两台都没领过），两台先各领到第 2 行。
    client.post("/api/script", json=script(["甲", "乙", "丙", "换景", "戊"]))
    create_stop(client, "换景")
    fetch(client, 1, 0)  # p1 -> 1
    fetch(client, 2, 0)  # p2 -> 1
    fetch(client, 1, 1)  # p1 -> 2
    fetch(client, 2, 1)  # p2 -> 2

    # 先验证正向移动：口令唯一地移到未领的第 3 行，停点随行移动。
    r = client.post(
        "/api/script",
        json=script(["甲", "乙", "换景", "丁改", "戊改"]),
    )
    assert r.status_code == 200
    assert pending_stop(client).json()["line_no"] == 3

    # 试图把口令移到已领的第 2 行：与已领行文字"乙"冲突，整稿 409 退回。
    r = client.post(
        "/api/script",
        json=script(["甲", "换景", "丙改", "丁改", "戊改"]),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "line_mismatch"
    assert r.json()["detail"]["line_no"] == 2
    # 停点仍在第 3 行，现役稿仍是移动后的 5 行版本，两台进度不变。
    assert pending_stop(client).json()["line_no"] == 3
    assert client.get("/api/script").json()["total_lines"] == 5
    progress = {p["prompter_id"]: p["last_line_no"] for p in client.get("/api/progress").json()}
    assert progress == {1: 2, 2: 2}


def test_restart_persists_pending_stop_and_arrivals(client):
    """重启后沿用待放行停点（含到台记录）与两台进度。"""
    client.post("/api/script", json=script(["甲", "停", "丙", "丁"]))
    stop = create_stop(client, "停").json()
    stop_id = stop["stop_id"]
    fetch(client, 1, 0)
    fetch(client, 2, 0)
    fetch(client, 1, 1)  # p1 先到 -> waiting，进度仍为 1
    fetch(client, 2, 1)  # p2 领到停点行 -> 进度 2
    assert pending_stop(client).json()["arrived_prompter_ids"] == [1, 2]

    main = client.main  # type: ignore[attr-defined]
    db_path = client.db_path  # type: ignore[attr-defined]
    del client

    importlib.reload(main)
    assert main.DB_PATH == db_path
    main.init_db()
    with TestClient(main.app) as restarted:
        pending = restarted.get("/api/stops/pending").json()
        assert pending["stop_id"] == stop_id
        assert pending["cue_text"] == "停"
        assert pending["line_no"] == 2
        assert pending["status"] == "pending"
        assert pending["arrived_prompter_ids"] == [1, 2]
        # 进度沿用：p1=1, p2=2。
        progress = {p["prompter_id"]: p["last_line_no"] for p in restarted.get("/api/progress").json()}
        assert progress == {1: 1, 2: 2}
        # 放行前 p1 仍等候。
        r = restarted.post("/api/prompters/1/lines", json={"last_line_no": 1})
        assert r.json()["status"] == "waiting"
        # 凭原停点编号放行。
        r = restarted.post(f"/api/stops/{stop_id}/release")
        assert r.status_code == 200
        # 放行后 p1 用原请求领到停点行。
        r = restarted.post("/api/prompters/1/lines", json={"last_line_no": 1})
        assert r.json()["line"] == {"line_no": 2, "text": "停"}


def test_released_stop_blocks_nothing_and_history_kept(client):
    client.post("/api/script", json=script(["甲", "停", "丙"]))
    stop_id = create_stop(client, "停").json()["stop_id"]
    fetch(client, 1, 0)
    fetch(client, 2, 0)
    fetch(client, 1, 1)  # p1 waiting
    fetch(client, 2, 1)  # p2 领到第 2 行
    release(client, stop_id)
    # 重复放行已放行停点 -> 409。
    r = release(client, stop_id)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "stop_already_released"
    # 历史列表保留，状态为 released。
    hist = {s["stop_id"]: s for s in client.get("/api/stops").json()}
    assert hist[stop_id]["status"] == "released"
