# 演出提词器服务（FastAPI + SQLite）

演出中途，导演可以随时提交一份**完整提词稿**；两台提词器各自凭"上次领到的行号"
顺序取下一行。已领成功的请求重发幂等返回原行，跳号请求拒绝；到稿尾先报"暂无"，
续稿后可用原请求继续领词。换景前，舞监可按口令原文设**停点**：两台提词器都要停在
同一句口令上，到齐后舞监凭停点编号放行。所有状态落在 SQLite，重启后沿用当前稿件、
各台进度与待放行停点。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   └── main.py            # FastAPI 应用、SQLite 持久化、全部业务规则（含停点/放行）
├── tests/
│   └── test_api.py        # 24 个 pytest 用例（TestClient，含重启模拟）
├── scripts/
│   └── e2e.sh             # 对运行中服务的端到端 HTTP 自测脚本（63 项检查）
├── Dockerfile
├── docker-compose.yml     # 单服务 + 命名卷 prompter-data 持久化 /data/prompter.db
├── requirements.txt
└── README.md
```

## 快速开始（Docker Compose）

```bash
docker compose up -d --build
# 打开交互文档（可直接在页面上设停点、交稿、领词、放行）
open http://localhost:8000/docs      # Linux: xdg-open http://localhost:8000/docs
curl http://localhost:8000/health    # {"status":"ok"}
```

数据库存放在命名卷 `prompter-data`（容器内 `/data/prompter.db`），
`docker compose down` 后数据仍在；要彻底清空：`docker compose down -v`。

### 不用 Docker 的本地运行方式

```bash
pip install -r requirements.txt
export PROMPTER_DB=./prompter.db          # 默认为 /data/prompter.db
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

> 注意：必须单 worker 运行。进程内 `threading.RLock` 与 SQLite 写事务配合，
> 保证领词/设停点/放行/换稿的"读-改-写"原子性；多 worker 进程各持一把锁会破坏该保证。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/api/script` | 当前稿件行数与版本号 |
| POST | `/api/script` | 提交完整提词稿（首稿 / 校对新稿 / 稿尾续写同一接口） |
| POST | `/api/prompters/{1\|2}/lines` | 领词（body 带 `last_line_no`，开场传 0） |
| GET | `/api/prompters/{1\|2}/progress` | 查单台进度 |
| GET | `/api/progress` | 查两台进度 |
| POST | `/api/stops` | 舞监按口令原文设换景停点 |
| GET | `/api/stops/pending` | 查当前待放行停点（无则 `null`） |
| GET | `/api/stops` | 查全部停点（含已放行历史） |
| POST | `/api/stops/{id}/release` | 两台到齐后凭停点编号放行 |

### 领词语义（基础规则）

设某台服务端进度为 `current`（已领到的最大行号，开场为 0），请求带 `last_line_no`：

| 条件 | 行为 |
| --- | --- |
| `last_line_no > current` | **409 跳号拒绝**（`reason=gap`），进度不变 |
| `last_line_no < current` | **重发已成功的请求**：幂等返回当前已领到的原行，不推进 |
| `last_line_no == current` 且下一行存在且非停点阻挡 | 领下一行，进度 +1，返回 `status=ok` |
| `last_line_no == current` 且已到稿尾 | 返回 `status=no_line`，**不推进**；续稿后同一请求即可领到新行 |
| 命中待放行停点（见下） | 先到者 `status=waiting` 进度不动；后到者照常领到停点行 |

普通领词响应：

```json
{ "status": "ok", "prompter_id": 1, "last_line_no": 2,
  "line": { "line_no": 2, "text": "第二句" }, "stop": null }
```

等候响应（多带 `stop` 字段）：

```json
{ "status": "waiting", "prompter_id": 1, "last_line_no": 1, "line": null,
  "stop": { "stop_id": 1, "cue_text": "换景口令", "line_no": 2,
            "status": "pending", "arrived_prompter_ids": [1] } }
```

### 换景停点与放行（核心规则）

**设停点** `POST /api/stops`，body 为 `{"cue_text": "口令原文"}`：

* 现役稿中必须**恰有一行**文字与口令完全一致（0 行或多行都 409，`reason=cue_not_unique`）；
* 该行两台提词器都还没领到（`max(两台进度) < 停点行号`），否则 409（`reason=line_already_taken`）；
* 同一时间只允许一个**待放行**停点，已存在时 409（`reason=pending_stop_exists`）；
* 成功返回 201 与停点编号 `stop_id`、定位到的 `line_no`、到台名单（初始为空）。
* 定位走 `script_lines.text` 索引的等值查询，按行号直接取停点行，**不扫描整稿**。

**领词遇到停点**（停点行号 = S）：

* 先到的一台取下一行正好是 S 时：登记到台，返回 `status=waiting`，**进度不动**；
  放行前它的任何领词请求（含重发历史请求）都继续 waiting；
* 另一台随后取下一行到 S 时：**照常领到停点行**（`status=ok`，进度推进到 S），
  同样登记到台；放行前它的后续请求也一律 waiting；
* 舞监 `POST /api/stops/{stop_id}/release`：
  - 两台未到齐 -> 409 `reason=not_all_arrived`；
  - 编号不存在 / 与当前待放行停点不符 -> 409（`stop_not_found` / `stop_id_mismatch`）；
  - 已放行的停点重复放行 -> 409 `stop_already_released`；
  - 任何拒绝都**不改变停点与进度**；
* 放行成功后停点不再阻挡，两台用各自原来的 `last_line_no` 请求即可继续领词
  （先到者此时领到停点行，后到者继续领下一行）。

### 放行前交新稿（换稿与停点联动）

`POST /api/script` 仍先做结构校验与已领行校验（见下），存在待放行停点时追加：

1. 新稿中停点口令原文仍须**恰有一行**，否则整稿 409（`cue_not_unique`）退回；
2. **两台都未到**停点时：停点可随口令移动，但目标行必须两台都没领过
   （`new_stop_line > max(两台进度)`），换稿事务内原子更新停点行号；
3. **任一台已到**停点时：停点固定在原行号，新稿口令必须仍落在该行，
   否则 409（`reason=stop_line_fixed`）整稿退回；
4. 任何冲突都是整稿退回，现役稿、停点（编号/行号/到台记录）与两台进度全部照旧。

### 换稿/续稿的基础规则

`POST /api/script` 的 body 是整份稿件，行号必须从 1 起连续、无重复无缺号。
服务端先做结构校验，再把新稿与**两台提词器已领行**（取两台进度最大值）逐行比对：

* 任一台领过的行，新稿中必须存在且**行号、文字完全一致**；
* 首处不符即返回 **409**，`detail.line_no` 指出第一个不符的行号，整份新稿退回，
  原稿继续供词、各台进度不变；
* 全部通过后在一个 SQLite 事务内原子替换稿件、必要时移动停点并递增 `version`。

因此"续稿"= 提交一份前缀与现稿完全相同、尾部追加新行的完整稿件；
"校对改词"若触及任何一台已领的行，必然被拒绝。

## 数据模型（SQLite）

```sql
CREATE TABLE script_lines ( line_no INTEGER PRIMARY KEY, text TEXT NOT NULL );
CREATE INDEX idx_script_lines_text ON script_lines(text);     -- 口令等值定位
CREATE TABLE prompters    ( prompter_id INTEGER PRIMARY KEY, last_line_no INTEGER NOT NULL DEFAULT 0 );
CREATE TABLE meta         ( key TEXT PRIMARY KEY, value TEXT NOT NULL );  -- version
CREATE TABLE stops        ( stop_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            cue_text TEXT NOT NULL, line_no INTEGER NOT NULL,
                            status TEXT NOT NULL CHECK (status IN ('pending','released')),
                            created_version INTEGER NOT NULL, released_version INTEGER );
CREATE TABLE stop_arrivals( stop_id INTEGER NOT NULL, prompter_id INTEGER NOT NULL,
                            PRIMARY KEY (stop_id, prompter_id) );
```

WAL 模式；每次请求独立连接、短事务。写操作（领词推进、设停点、放行、换稿）
由进程内 `RLock` 串行化，读操作不加锁。停点编号由 SQLite 自增主键生成，
停点与到台记录随数据库持久化，重启后完整沿用。

## 在 /docs 上的试用流程

1. `POST /api/script` 提交 4 行稿件（如 `开场白 / 换景口令 / 第三句 / 第四句`）；
2. `POST /api/stops` 传 `{"cue_text":"换景口令"}` 设停点，记录返回的 `stop_id`；
3. 两台分别 `POST /api/prompters/{id}/lines` 领第 1 行；
4. 1 号机再领下一行，观察 `status=waiting`、进度不动、`stop.arrived_prompter_ids=[1]`；
5. 先试 `POST /api/stops/{stop_id}/release`，观察 409 `not_all_arrived`；
6. 2 号机领下一行，观察它正常 `status=ok` 拿到停点行，到台名单变为 `[1,2]`；
7. 再次放行，观察 `status=released`；两台用原来的 `last_line_no` 继续领词；
8. 设新停点后、放行前 `POST /api/script` 交新稿，分别试口令移动（未到齐）、
   口令重复（退回）、一台到后口令移位（`stop_line_fixed` 退回）。

## 实测结果

测试环境：Python 3.11.2 / FastAPI 0.165.0 / uvicorn 0.54.0 / SQLite（系统自带）。

### 1. 单元/接口测试：`pytest tests/` —— 24 passed

在原有 14 个用例（领词、幂等、跳号、稿尾续写、换稿冲突、结构校验、重启持久化等）
之外，新增 10 个停点专用用例，覆盖：

* 设停点三类拒绝（口令 0 行 / 多行匹配、停点行已被领取、同时存在两个待放行停点）；
* **一台先到**：先到者 waiting 且进度不动（含重发历史请求也 waiting）、提前放行与
  错号放行退回、另一台照常领到停点行并推进、到齐凭编号放行、两台用原请求续领；
* 先到者互换（2 号机先到同样成立）；
* **领词与换稿交错**：两台都未到时停点随口令移动到未领行、口令不唯一整稿退回、
  一台到达后停点固定原行号（移位即 `stop_line_fixed` 退回）、移动目标为已领行的
  保护分支；
* 重启后待放行停点、到台名单、两台进度完整沿用，放行前后行为一致；
* 已放行停点重复放行 409、历史停点列表保留。

### 2. 真实 HTTP 端到端：`bash scripts/e2e.sh` —— PASS=63 FAIL=0

对运行中的 uvicorn 进程执行完整业务流，第 11–14 段专门覆盖停点：
设停点校验、一台先到/提前与错号放行退回、另一台领停点行、到齐放行、原请求续领，
以及放行前换稿的移动/固定/整稿退回交错场景。

### 3. 真实进程重启测试

在"停点 3 待放行、1 号机已到（进度 12）、2 号机进度 12、稿件 14 行 version=8"
状态下杀掉 uvicorn 并用同一数据库文件重启：待放行停点（编号、口令、行号 13、
到台名单 `[1]`）与两台进度完整保留；1 号机放行前仍 waiting、提前放行仍 409；
2 号机到达领到第 13 行后，凭原停点编号放行成功，1 号机用原请求领到停点行。

### 4. 并发测试（停点处）

独立数据库上 12 线程栅栏并发：

* 12 个相同请求同时让先到机命中停点行：全部 `waiting`，进度不动，到台表只插入 1 条；
* 12 个相同请求同时让另一台命中停点行：**恰好 1 个** `ok` 领到停点行并推进，
  其余 11 个 `waiting`，到台名单恰为 `[1,2]`，无重复推进、无跳号；
* 放行后 12 个相同原请求并发：全部幂等返回停点行，进度只推进一次。

## 遗留项 / 设计约束

1. **单 worker 约束**：原子性依赖进程内锁，水平扩容需改为多进程方案
   （如 PostgreSQL 行锁 `SELECT ... FOR UPDATE`，或为每台提词器建唯一进度行做原子 UPDATE）。
2. **重发与重复请求在协议上同形**：服务端只能凭 `last_line_no` 与当前进度的关系判断，
   无法区分网络重试与客户端重复点击；两者都按幂等返回当前行处理。客户端要领下一行时，
   应使用上次响应中返回的 `last_line_no` 作为新请求参数。
3. **无鉴权**：按题意假设内网可信环境，导演/舞监接口与提词器接口均未加认证；
   上生产前需补 API Key / 网络隔离。
4. **换稿为全量替换**：不支持 diff 式补丁，导演每次须提交整份稿件（题意如此）。
5. **"停点移到已领行"在数据上不可达**：已领行的行号+文字被换稿规则锁定，而口令在
   旧稿中唯一，任何把口令挪到已领行的新稿都会先触发 `line_mismatch`；
   `line_already_taken` 停点移动分支作为防御性校验保留并由测试间接覆盖。
6. 测试依赖（pytest/httpx）未写入 `requirements.txt`，运行时镜像只含生产依赖。
7. 本环境未安装 Docker，`Dockerfile` 与 `docker-compose.yml` 已通过 YAML/结构校验，
   但容器化启动未在本机实际执行；所有功能测试均在本机 uvicorn + SQLite 上完成。
