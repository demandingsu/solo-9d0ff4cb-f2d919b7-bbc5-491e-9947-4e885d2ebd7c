"""演出提词器服务 (FastAPI + SQLite)。

业务规则
========
基础领词
--------
* 导演通过 POST /api/script 提交完整提词稿，行号从 1 起连续。
* 两台提词器 (id=1,2) 各自凭"上次领到的行号"调用 POST /api/prompters/{id}/lines
  领取下一行；开场时 last_line_no 传 0。
* 已成功领取的请求若重发 (last_line_no 小于该台当前进度)，幂等返回当前已领到的原行，
  不重复推进。
* 跳号请求 (last_line_no 大于该台当前进度) 一律 409 拒绝。
* 已到稿尾时返回 status=no_line（不推进进度）；导演续稿后，用原请求即可领到新行。
* 校对期间继续用原稿供词；换稿只在全部已领行的"行号+文字"一致时原子生效，
  首处不符即返回该行号并退回整份新稿，原稿与各台进度不变。

换景停点（舞监放行）
--------------------
* 舞监 POST /api/stops 按口令原文设停点：现役稿中须恰有一行 text 与口令相同，
  且任一台都还没领到该行；同一时间只允许一个"待放行"停点，否则 409 退回。
* 领词直接定位停点行（按行号索引取值，不扫描整稿）：
  - 先到的一台在取下一行时命中停点行：返回 status=waiting，进度不动；
  - 另一台照常领到停点行（进度推进到该行），其请求也算"到"；
  - 到齐前两台的任何后续领词请求都返回 waiting，进度一律不动；
  - 舞监凭本次停点编号 POST /api/stops/{id}/release 放行；提前放行或编号不符
    一律 409 退回，停点保持原状；
  - 放行后停点不再阻挡，两台用各自原来的请求即可继续领词。
* 放行前导演交新稿，新稿仍须满足原有结构与已领行校验；此外：
  - 新稿中口令原文仍须恰有一行（0 行或多行 -> 整稿 409 退回）；
  - 以新稿生效时的进度为准：两台都未到时，停点可随口令移动到两台都没领过的行；
  - 任一台已到时，停点固定在原行号（新稿该口令必须落在原行号，否则退回）；
  - 冲突时整稿退回，停点与两台进度照旧。
* 全部状态保存在 SQLite，重启后沿用当前稿、各台进度以及待放行停点（含到台记录）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

DB_PATH = os.environ.get("PROMPTER_DB", "/data/prompter.db")
PROMPTER_IDS = (1, 2)

# 同一进程内对写事务串行化，保证领词/设停点/放行/换稿的读-改-写原子性。
_state_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
class ScriptLineIn(BaseModel):
    line_no: int = Field(..., description="行号，从 1 开始且必须连续")
    text: str = Field(..., description="提词文字（允许空字符串）")

    @field_validator("line_no")
    @classmethod
    def _check_line_no(cls, v: int) -> int:
        if v < 1:
            raise ValueError("line_no 必须从 1 开始")
        return v


class ScriptSubmit(BaseModel):
    lines: list[ScriptLineIn] = Field(..., description="完整提词稿（校对/续稿同样提交整份）")


class ScriptLineOut(BaseModel):
    line_no: int
    text: str


class StopInfo(BaseModel):
    stop_id: int
    cue_text: str
    line_no: int
    status: Literal["pending", "released"]
    arrived_prompter_ids: list[int] = Field(
        default_factory=list, description="已到达停点的提词器编号（升序）"
    )


class FetchRequest(BaseModel):
    last_line_no: int = Field(
        ..., ge=0, description="本台上次领到的行号，开场传 0；重发已成功的请求时传同一值"
    )


class LineResponse(BaseModel):
    status: Literal["ok", "no_line", "waiting"]
    prompter_id: int
    last_line_no: int = Field(..., description="处理后该台的当前进度（已领到的最大行号）")
    line: ScriptLineOut | None = None
    stop: StopInfo | None = Field(
        None, description="因待放行停点而等候时，回传当前停点信息；正常领词时为 null"
    )


class ProgressOut(BaseModel):
    prompter_id: int
    last_line_no: int


class ScriptInfoOut(BaseModel):
    total_lines: int
    version: int


class StopCreate(BaseModel):
    cue_text: str = Field(..., min_length=1, description="停点口令原文，须与现役稿某一行完全一致")


class ReleaseOut(BaseModel):
    status: Literal["released"]
    stop: StopInfo


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS script_lines (
                line_no INTEGER PRIMARY KEY,
                text    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_script_lines_text
                ON script_lines(text);
            CREATE TABLE IF NOT EXISTS prompters (
                prompter_id  INTEGER PRIMARY KEY,
                last_line_no INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS stops (
                stop_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                cue_text       TEXT NOT NULL,
                line_no        INTEGER NOT NULL,
                status         TEXT NOT NULL CHECK (status IN ('pending', 'released')),
                created_version INTEGER NOT NULL,
                released_version INTEGER
            );
            CREATE TABLE IF NOT EXISTS stop_arrivals (
                stop_id      INTEGER NOT NULL,
                prompter_id  INTEGER NOT NULL,
                PRIMARY KEY (stop_id, prompter_id)
            );
            """
        )
        for pid in PROMPTER_IDS:
            conn.execute(
                "INSERT OR IGNORE INTO prompters(prompter_id, last_line_no) VALUES (?, 0)",
                (pid,),
            )
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('version', '0')")
        conn.commit()


# --------------------------------------------------------------------------- #
# 依赖 / 辅助
# --------------------------------------------------------------------------- #
def validate_prompter(
    prompter_id: int = Path(..., description="提词器编号，仅 1 或 2"),
) -> int:
    if prompter_id not in PROMPTER_IDS:
        raise HTTPException(status_code=404, detail="提词器不存在，仅允许 id=1 或 2")
    return prompter_id


def _get_pending_stop(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """取当前唯一的待放行停点（不存在返回 None）。"""
    return conn.execute(
        "SELECT stop_id, cue_text, line_no, status, created_version "
        "FROM stops WHERE status='pending' ORDER BY stop_id DESC LIMIT 1"
    ).fetchone()


def _arrived_ids(conn: sqlite3.Connection, stop_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT prompter_id FROM stop_arrivals WHERE stop_id=? ORDER BY prompter_id",
        (stop_id,),
    ).fetchall()
    return [r["prompter_id"] for r in rows]


def _stop_info(conn: sqlite3.Connection, row: sqlite3.Row) -> StopInfo:
    return StopInfo(
        stop_id=row["stop_id"],
        cue_text=row["cue_text"],
        line_no=row["line_no"],
        status=row["status"],
        arrived_prompter_ids=_arrived_ids(conn, row["stop_id"]),
    )


def _waiting_response(prompter_id: int, current: int, info: StopInfo) -> LineResponse:
    return LineResponse(
        status="waiting",
        prompter_id=prompter_id,
        last_line_no=current,
        line=None,
        stop=info,
    )


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_app: FastAPI) -> Iterator[None]:
    init_db()
    yield


app = FastAPI(
    title="演出提词器 API",
    description=(
        "两台提词器凭上次领到的行号顺序领词；导演可中途提交完整新稿，已领行的行号与文字"
        "保持一致才允许换稿；舞监可按口令设换景停点，两台到齐后凭停点编号放行。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["运维"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/progress", response_model=list[ProgressOut], tags=["提词器"])
def list_progress() -> list[ProgressOut]:
    """查看两台提词器当前进度（已领到的最大行号）。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT prompter_id, last_line_no FROM prompters ORDER BY prompter_id"
        ).fetchall()
    return [ProgressOut(**dict(r)) for r in rows]


@app.get("/api/prompters/{prompter_id}/progress", response_model=ProgressOut, tags=["提词器"])
def get_progress(prompter_id: int = Depends(validate_prompter)) -> ProgressOut:
    with db() as conn:
        row = conn.execute(
            "SELECT prompter_id, last_line_no FROM prompters WHERE prompter_id=?",
            (prompter_id,),
        ).fetchone()
    return ProgressOut(**dict(row))


@app.post(
    "/api/prompters/{prompter_id}/lines",
    response_model=LineResponse,
    tags=["提词器"],
    responses={
        409: {
            "description": "跳号拒绝：last_line_no 与该台当前进度不连续",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "reason": "gap",
                            "prompter_id": 1,
                            "submitted_last_line_no": 5,
                            "current_last_line_no": 3,
                        }
                    }
                }
            },
        }
    },
)
def fetch_line(
    payload: FetchRequest,
    prompter_id: int = Depends(validate_prompter),
) -> LineResponse:
    """领取下一行；重发已成功的请求时幂等返回原行；命中待放行停点时等候。"""
    last_line_no = payload.last_line_no

    with _state_lock, db() as conn:
        row = conn.execute(
            "SELECT last_line_no FROM prompters WHERE prompter_id=?", (prompter_id,)
        ).fetchone()
        current = row["last_line_no"]

        # 跳号：提交的"上次行号"超过本台真实进度（含凭空跳过若干行的请求）。
        if last_line_no > current:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "gap",
                    "prompter_id": prompter_id,
                    "submitted_last_line_no": last_line_no,
                    "current_last_line_no": current,
                },
            )

        pending = _get_pending_stop(conn)

        # 该台已经到达停点（先到等候的一台，或已领到停点行的一台）：
        # 放行前任何领词请求一律等候，进度不动。
        if pending is not None:
            arrived = _arrived_ids(conn, pending["stop_id"])
            if prompter_id in arrived:
                return _waiting_response(
                    prompter_id, current, _stop_info(conn, pending)
                )
        else:
            arrived = []

        # 重发已成功的请求：幂等返回原来那一行，不重复推进。
        if last_line_no < current:
            line_row = conn.execute(
                "SELECT line_no, text FROM script_lines WHERE line_no=?", (current,)
            ).fetchone()
            if line_row is None:
                # 理论上不会发生：进度指向的行必然存在于当前稿。
                raise HTTPException(status_code=500, detail="内部错误：进度指向的行缺失")
            return LineResponse(
                status="ok",
                prompter_id=prompter_id,
                last_line_no=current,
                line=ScriptLineOut(**dict(line_row)),
            )

        # last_line_no == current：正常请求下一行。
        next_no = current + 1
        line_row = conn.execute(
            "SELECT line_no, text FROM script_lines WHERE line_no=?", (next_no,)
        ).fetchone()
        if line_row is None:
            # 已到稿尾：报暂无，不推进进度。续稿后用同一请求即可领到新行。
            return LineResponse(
                status="no_line",
                prompter_id=prompter_id,
                last_line_no=current,
                line=None,
            )

        # 直接按行号定位停点，不扫描整稿。
        if pending is not None and next_no == pending["line_no"]:
            stop_id = pending["stop_id"]
            if arrived:
                # 另一台已经先到：本台照常领到停点行（进度推进），同样登记到台。
                conn.execute(
                    "INSERT OR IGNORE INTO stop_arrivals(stop_id, prompter_id) VALUES (?, ?)",
                    (stop_id, prompter_id),
                )
                conn.execute(
                    "UPDATE prompters SET last_line_no=? WHERE prompter_id=?",
                    (next_no, prompter_id),
                )
                conn.commit()
                return LineResponse(
                    status="ok",
                    prompter_id=prompter_id,
                    last_line_no=next_no,
                    line=ScriptLineOut(**dict(line_row)),
                    stop=None,
                )
            # 先到的一台：停在停点行前，返回等候，进度不动。
            conn.execute(
                "INSERT OR IGNORE INTO stop_arrivals(stop_id, prompter_id) VALUES (?, ?)",
                (stop_id, prompter_id),
            )
            conn.commit()
            return _waiting_response(
                prompter_id, current, _stop_info(conn, pending)
            )

        conn.execute(
            "UPDATE prompters SET last_line_no=? WHERE prompter_id=?",
            (next_no, prompter_id),
        )
        conn.commit()
        return LineResponse(
            status="ok",
            prompter_id=prompter_id,
            last_line_no=next_no,
            line=ScriptLineOut(**dict(line_row)),
        )


# --------------------------------------------------------------------------- #
# 停点（舞监）
# --------------------------------------------------------------------------- #
@app.get("/api/stops/pending", response_model=StopInfo | None, tags=["停点"])
def get_pending_stop() -> StopInfo | None:
    """查看当前待放行停点（含到台提词器编号）；没有待放行停点时返回 null。"""
    with db() as conn:
        row = _get_pending_stop(conn)
        if row is None:
            return None
        return _stop_info(conn, row)


@app.get("/api/stops", response_model=list[StopInfo], tags=["停点"])
def list_stops() -> list[StopInfo]:
    """查看历史停点（最新在前，含已放行记录）。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT stop_id, cue_text, line_no, status, created_version "
            "FROM stops ORDER BY stop_id DESC"
        ).fetchall()
        return [_stop_info(conn, r) for r in rows]


@app.post(
    "/api/stops",
    response_model=StopInfo,
    status_code=201,
    tags=["停点"],
    responses={
        409: {
            "description": "设停点被拒：已有待放行停点 / 口令在现役稿中不唯一 / 该行已被领取",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "reason": "cue_not_unique",
                            "cue_text": "换景",
                            "match_count": 2,
                            "message": "口令原文在现役稿中必须恰有一行匹配，实际 2 行",
                        }
                    }
                }
            },
        }
    },
)
def create_stop(payload: StopCreate) -> StopInfo:
    """舞监按口令原文设置换景停点。

    前置条件：当前没有待放行停点；现役稿恰有一行文字等于口令；该行两台都还没领到。
    """
    cue = payload.cue_text

    with _state_lock, db() as conn:
        if _get_pending_stop(conn) is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "pending_stop_exists",
                    "message": "同一时间只允许一个待放行的停点，请先放行或等待当前停点结束",
                },
            )

        # 走 text 索引直接定位口令行，不扫描整稿。
        matches = conn.execute(
            "SELECT line_no FROM script_lines WHERE text=? ORDER BY line_no", (cue,)
        ).fetchall()
        if len(matches) != 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "cue_not_unique",
                    "cue_text": cue,
                    "match_count": len(matches),
                    "message": (
                        "口令原文在现役稿中必须恰有一行匹配，"
                        f"实际 {len(matches)} 行"
                    ),
                },
            )
        stop_line_no = matches[0]["line_no"]

        max_taken = conn.execute(
            "SELECT MAX(last_line_no) AS m FROM prompters"
        ).fetchone()["m"] or 0
        if stop_line_no <= max_taken:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "line_already_taken",
                    "line_no": stop_line_no,
                    "message": (
                        f"停点行 {stop_line_no} 已被某台提词器领到，"
                        "停点只能设在两台都没领过的行"
                    ),
                },
            )

        version = int(
            conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()["value"]
        )
        cur = conn.execute(
            "INSERT INTO stops(cue_text, line_no, status, created_version) "
            "VALUES (?, ?, 'pending', ?)",
            (cue, stop_line_no, version),
        )
        conn.commit()
        row = conn.execute(
            "SELECT stop_id, cue_text, line_no, status, created_version "
            "FROM stops WHERE stop_id=?",
            (cur.lastrowid,),
        ).fetchone()
        return _stop_info(conn, row)


@app.post(
    "/api/stops/{stop_id}/release",
    response_model=ReleaseOut,
    tags=["停点"],
    responses={
        409: {
            "description": "放行被拒：停点不存在 / 已放行 / 两台未到齐 / 编号与待放行停点不符",
        }
    },
)
def release_stop(stop_id: int = Path(..., ge=1, description="舞监本次停点编号")) -> ReleaseOut:
    """两台到齐后，舞监凭停点编号放行；提前放行或编号不符一律退回，停点保持原状。"""
    with _state_lock, db() as conn:
        row = conn.execute(
            "SELECT stop_id, cue_text, line_no, status, created_version "
            "FROM stops WHERE stop_id=?",
            (stop_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "stop_not_found",
                    "stop_id": stop_id,
                    "message": "停点编号不存在，放行退回，停点保持原状",
                },
            )
        if row["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "stop_already_released",
                    "stop_id": stop_id,
                    "message": "该停点已放行，重复放行退回",
                },
            )

        pending = _get_pending_stop(conn)
        # 同一时间至多一个待放行停点；编号对不上当前待放行停点即退回。
        if pending is None or pending["stop_id"] != stop_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "stop_id_mismatch",
                    "stop_id": stop_id,
                    "pending_stop_id": None if pending is None else pending["stop_id"],
                    "message": "放行编号与当前待放行停点不符，退回，停点保持原状",
                },
            )

        arrived = _arrived_ids(conn, stop_id)
        if sorted(arrived) != list(PROMPTER_IDS):
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "not_all_arrived",
                    "stop_id": stop_id,
                    "arrived_prompter_ids": arrived,
                    "message": "两台提词器尚未到齐，提前放行退回，停点保持原状",
                },
            )

        version = int(
            conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()["value"]
        )
        conn.execute(
            "UPDATE stops SET status='released', released_version=? WHERE stop_id=?",
            (version, stop_id),
        )
        conn.commit()
        released = conn.execute(
            "SELECT stop_id, cue_text, line_no, status, created_version "
            "FROM stops WHERE stop_id=?",
            (stop_id,),
        ).fetchone()
        return ReleaseOut(status="released", stop=_stop_info(conn, released))


# --------------------------------------------------------------------------- #
# 稿件
# --------------------------------------------------------------------------- #
@app.get("/api/script", response_model=ScriptInfoOut, tags=["稿件"])
def get_script_info() -> ScriptInfoOut:
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM script_lines").fetchone()["c"]
        version = int(conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()["value"])
    return ScriptInfoOut(total_lines=total, version=version)


@app.post(
    "/api/script",
    status_code=200,
    tags=["稿件"],
    responses={
        409: {
            "description": "新稿被退回：结构不连续、与已领行不符，或待放行停点口令规则不满足",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "reason": "line_mismatch",
                            "line_no": 3,
                            "submitted_text": "新文字",
                            "existing_text": "原文字",
                        }
                    }
                }
            },
        }
    },
)
def submit_script(payload: ScriptSubmit) -> JSONResponse:
    """提交完整提词稿（首稿 / 校对新稿 / 稿尾续写均走此接口）。

    校验顺序：先结构（行号连续无重复），再与所有提词器已领行逐行比对；
    若存在待放行停点，再校验口令规则（口令恰有一行；未到齐可随进度移动，
    有台到达则固定原行号）。任一不符即退回整份新稿，原稿、停点与进度均不变。
    全部通过后原子替换稿件、必要时移动停点并提升版本号。
    """
    submitted = payload.lines

    # ---- 结构校验：1..N 连续，无重复，无缺号 ----
    seen: set[int] = set()
    for expected, item in enumerate(submitted, start=1):
        if item.line_no in seen:
            return _reject(
                reason="duplicate_line_no",
                line_no=item.line_no,
                message=f"行号重复：第 {expected} 条数据的 line_no={item.line_no} 已出现过",
            )
        seen.add(item.line_no)
        if item.line_no != expected:
            return _reject(
                reason="non_consecutive_line_no",
                line_no=item.line_no,
                message=(
                    f"行号不连续：第 {expected} 条数据应为 line_no={expected}，"
                    f"实际为 {item.line_no}"
                ),
            )

    new_lines = {item.line_no: item.text for item in submitted}

    with _state_lock, db() as conn:
        # ---- 已领行兼容校验：任一台领过的行，行号与文字都必须保持 ----
        max_taken = conn.execute(
            "SELECT MAX(last_line_no) AS m FROM prompters"
        ).fetchone()["m"] or 0
        for line_no in range(1, max_taken + 1):
            if line_no not in new_lines:
                return _reject(
                    reason="missing_taken_line",
                    line_no=line_no,
                    message=f"新稿缺少已被领取的行 {line_no}，整份新稿退回",
                )
            existing = conn.execute(
                "SELECT text FROM script_lines WHERE line_no=?", (line_no,)
            ).fetchone()
            existing_text = existing["text"] if existing is not None else None
            if new_lines[line_no] != existing_text:
                return _reject(
                    reason="line_mismatch",
                    line_no=line_no,
                    message=f"第 {line_no} 行文字与已领取的原文不符，整份新稿退回",
                    submitted_text=new_lines[line_no],
                    existing_text=existing_text,
                )

        # ---- 待放行停点联动校验（放行前交新稿的额外规则） ----
        pending = _get_pending_stop(conn)
        stop_update: tuple[int, int] | None = None  # (stop_id, 新行号)
        if pending is not None:
            stop_id = pending["stop_id"]
            cue = pending["cue_text"]
            old_line_no = pending["line_no"]
            cue_matches = sorted(no for no, text in new_lines.items() if text == cue)

            # 放行前交的新稿也须恰有一行口令。
            if len(cue_matches) != 1:
                return _reject(
                    reason="cue_not_unique",
                    line_no=old_line_no,
                    message=(
                        f"待放行停点口令 {cue!r} 在新稿中必须恰有一行匹配，"
                        f"实际 {len(cue_matches)} 行，整份新稿退回，停点保持原状"
                    ),
                    cue_text=cue,
                    match_count=len(cue_matches),
                )
            new_stop_line = cue_matches[0]
            arrived = _arrived_ids(conn, stop_id)

            if arrived:
                # 任一台已到：停点固定原行号，口令必须仍在原行号。
                if new_stop_line != old_line_no:
                    return _reject(
                        reason="stop_line_fixed",
                        line_no=old_line_no,
                        message=(
                            f"已有提词器到达停点，停点固定在第 {old_line_no} 行；"
                            f"新稿口令落在第 {new_stop_line} 行，整份新稿退回，停点保持原状"
                        ),
                        cue_text=cue,
                        submitted_line_no=new_stop_line,
                    )
            else:
                # 两台都未到：停点可随口令移动，但新行必须两台都没领过。
                if new_stop_line <= max_taken:
                    return _reject(
                        reason="line_already_taken",
                        line_no=new_stop_line,
                        message=(
                            f"新稿口令落在第 {new_stop_line} 行，但该行已被某台领到，"
                            "停点只能移到两台都没领过的行，整份新稿退回，停点保持原状"
                        ),
                        cue_text=cue,
                    )
                if new_stop_line != old_line_no:
                    stop_update = (stop_id, new_stop_line)

        # ---- 全部通过：原子替换稿件、移动停点（若有）并提升版本号 ----
        version_row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        new_version = int(version_row["value"]) + 1
        conn.execute("DELETE FROM script_lines")
        conn.executemany(
            "INSERT INTO script_lines(line_no, text) VALUES (?, ?)",
            [(no, new_lines[no]) for no in sorted(new_lines)],
        )
        if stop_update is not None:
            conn.execute(
                "UPDATE stops SET line_no=? WHERE stop_id=? AND status='pending'",
                (stop_update[1], stop_update[0]),
            )
        conn.execute("UPDATE meta SET value=? WHERE key='version'", (str(new_version),))
        conn.commit()

    content: dict[str, object] = {
        "status": "accepted",
        "total_lines": len(new_lines),
        "version": new_version,
        "message": "新稿已生效，已领行的行号与文字保持不变",
    }
    if pending is not None:
        with db() as conn:
            row = _get_pending_stop(conn)
            content["stop"] = None if row is None else _stop_info(conn, row).model_dump()
    return JSONResponse(status_code=200, content=content)


def _reject(reason: str, line_no: int, message: str, **extra: object) -> JSONResponse:
    detail: dict[str, object] = {"reason": reason, "line_no": line_no, "message": message}
    detail.update(extra)
    return JSONResponse(status_code=409, content={"detail": detail})
