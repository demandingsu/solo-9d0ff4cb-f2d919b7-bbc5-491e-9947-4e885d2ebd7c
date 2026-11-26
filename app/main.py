"""演出提词器服务 (FastAPI + SQLite)。

业务规则
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
* 全部状态保存在 SQLite，重启后沿用当前稿与各台进度。
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

# 同一进程内对写事务串行化，保证领词/换稿的读-改-写原子性。
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


class FetchRequest(BaseModel):
    last_line_no: int = Field(
        ..., ge=0, description="本台上次领到的行号，开场传 0；重发已成功的请求时传同一值"
    )


class LineResponse(BaseModel):
    status: Literal["ok", "no_line"]
    prompter_id: int
    last_line_no: int = Field(..., description="处理后该台的当前进度（已领到的最大行号）")
    line: ScriptLineOut | None = None


class ProgressOut(BaseModel):
    prompter_id: int
    last_line_no: int


class ScriptInfoOut(BaseModel):
    total_lines: int
    version: int


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
            CREATE TABLE IF NOT EXISTS prompters (
                prompter_id  INTEGER PRIMARY KEY,
                last_line_no INTEGER NOT NULL DEFAULT 0
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
# 依赖
# --------------------------------------------------------------------------- #
def validate_prompter(
    prompter_id: int = Path(..., description="提词器编号，仅 1 或 2"),
) -> int:
    if prompter_id not in PROMPTER_IDS:
        raise HTTPException(status_code=404, detail="提词器不存在，仅允许 id=1 或 2")
    return prompter_id


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
        "两台提词器凭上次领到的行号顺序领词；导演可中途提交完整新稿，"
        "已领行的行号与文字保持一致才允许换稿。"
    ),
    version="1.0.0",
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
    """领取下一行；重发已成功的请求时幂等返回原行。"""
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
            "description": "新稿被退回：结构不连续，或与已领行首处不符",
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
    首处不符即退回整份新稿，原稿继续供词。全部通过后原子替换稿件。
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
        progress_rows = conn.execute("SELECT MAX(last_line_no) AS m FROM prompters").fetchone()
        max_taken = progress_rows["m"] or 0
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

        # ---- 全部通过：原子替换稿件并提升版本号 ----
        version_row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        new_version = int(version_row["value"]) + 1
        conn.execute("DELETE FROM script_lines")
        conn.executemany(
            "INSERT INTO script_lines(line_no, text) VALUES (?, ?)",
            [(no, new_lines[no]) for no in sorted(new_lines)],
        )
        conn.execute("UPDATE meta SET value=? WHERE key='version'", (str(new_version),))
        conn.commit()

    return JSONResponse(
        status_code=200,
        content={
            "status": "accepted",
            "total_lines": len(new_lines),
            "version": new_version,
            "message": "新稿已生效，已领行的行号与文字保持不变",
        },
    )


def _reject(reason: str, line_no: int, message: str, **extra: object) -> JSONResponse:
    detail: dict[str, object] = {"reason": reason, "line_no": line_no, "message": message}
    detail.update(extra)
    return JSONResponse(status_code=409, content={"detail": detail})
