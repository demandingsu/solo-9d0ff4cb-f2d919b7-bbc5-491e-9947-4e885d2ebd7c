# 演出提词器服务（FastAPI + SQLite）

演出中途，导演可以随时提交一份**完整提词稿**；两台提词器各自凭"上次领到的行号"
顺序取下一行。已领成功的请求重发幂等返回原行，跳号请求拒绝；到稿尾先报"暂无"，
续稿后可用原请求继续领词。所有状态落在 SQLite，重启后沿用当前稿件与各台进度。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   └── main.py            # FastAPI 应用、SQLite 持久化、全部业务规则
├── tests/
│   └── test_api.py        # 14 个 pytest 用例（TestClient，含重启模拟）
├── scripts/
│   └── e2e.sh             # 对运行中服务的端到端 HTTP 自测脚本（23 项检查）
├── Dockerfile
├── docker-compose.yml     # 单服务 + 命名卷 prompter-data 持久化 /data/prompter.db
├── requirements.txt
└── README.md
```

## 快速开始（Docker Compose）

```bash
docker compose up -d --build
# 打开交互文档（可直接在页面上试领词、换稿）
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
> 保证领词/换稿的"读-改-写"原子性；多 worker 进程各持一把锁会破坏该保证。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/api/script` | 当前稿件行数与版本号 |
| POST | `/api/script` | 提交完整提词稿（首稿 / 校对新稿 / 稿尾续写同一接口） |
| POST | `/api/prompters/{1\|2}/lines` | 领词（body 带 `last_line_no`，开场传 0） |
| GET | `/api/prompters/{1\|2}/progress` | 查单台进度 |
| GET | `/api/progress` | 查两台进度 |

### 领词语义（核心规则）

设某台服务端进度为 `current`（已领到的最大行号，开场为 0），请求带 `last_line_no`：

| 条件 | 行为 |
| --- | --- |
| `last_line_no > current` | **409 跳号拒绝**（`reason=gap`），进度不变 |
| `last_line_no < current` | **重发已成功的请求**：幂等返回当前已领到的原行，不推进 |
| `last_line_no == current` 且下一行存在 | 领下一行，进度 +1，返回 `status=ok` |
| `last_line_no == current` 且已到稿尾 | 返回 `status=no_line`，**不推进**；续稿后同一请求即可领到新行 |

领词响应示例：

```json
{ "status": "ok", "prompter_id": 1, "last_line_no": 2,
  "line": { "line_no": 2, "text": "第二句" } }
```

稿尾响应：`{"status":"no_line","prompter_id":1,"last_line_no":2,"line":null}`

### 换稿/续稿规则

`POST /api/script` 的 body 是整份稿件，行号必须从 1 起连续、无重复无缺号。
服务端先做结构校验，再把新稿与**两台提词器已领行**（取两台进度最大值）逐行比对：

* 任一台领过的行，新稿中必须存在且**行号、文字完全一致**；
* 首处不符即返回 **409**，`detail.line_no` 指出第一个不符的行号，整份新稿退回，
  原稿继续供词、各台进度不变；
* 全部通过后在一个 SQLite 事务内原子替换稿件并递增 `version`。

因此"续稿"= 提交一份前缀与现稿完全相同、尾部追加新行的完整稿件；
"校对改词"若触及任何一台已领的行，必然被拒绝。

## 数据模型（SQLite）

```sql
CREATE TABLE script_lines ( line_no INTEGER PRIMARY KEY, text TEXT NOT NULL );
CREATE TABLE prompters    ( prompter_id INTEGER PRIMARY KEY, last_line_no INTEGER NOT NULL DEFAULT 0 );
CREATE TABLE meta         ( key TEXT PRIMARY KEY, value TEXT NOT NULL );  -- version
```

WAL 模式；每次请求独立连接、短事务。写操作（领词推进、换稿）由进程内
`RLock` 串行化，读操作不加锁。

## 在 /docs 上的试用流程

1. `POST /api/script` 提交 3 行稿件；
2. `POST /api/prompters/1/lines`，body `{"last_line_no": 0}` 领第 1 行；
   再发一次同样的请求，观察幂等返回同一行；
3. 把 `last_line_no` 改成 5 发送，观察 409 跳号拒绝；
4. 领到稿尾后观察 `status=no_line`，再 `POST /api/script` 提交追加行的完整稿，
   用原来的 `last_line_no` 再领一次，即可拿到续稿的新行；
5. 提交一份改动了已领行文字的稿件，观察 409 与 `detail.line_no`。

## 实测结果

测试环境：Python 3.11.2 / FastAPI 0.165.0 / uvicorn 0.54.0 / SQLite（系统自带）。

### 1. 单元/接口测试：`pytest tests/` —— 14 passed

覆盖：开场状态、顺序领词、双机独立、重发幂等（含任意历史值）、跳号 409、
稿尾 `no_line` 后续写、校对冲突首处不符报行号并整份退回、缺行/重复/缺号结构校验、
**两台交错领词中途换稿已领行行号文字不变**、换稿后行号延续、空稿与空字符串行、
非法提词器 id、以及重新加载模块（模拟重启）后稿件与两台进度沿用。

### 2. 真实 HTTP 端到端：`bash scripts/e2e.sh` —— PASS=23 FAIL=0

对运行中的 uvicorn 进程执行完整业务流（含交错换稿、稿尾续写、结构校验）。

### 3. 真实进程重启测试

杀掉 uvicorn 进程后用同一数据库文件重启：稿件（5 行、version=3）与两台进度
（p1=5、p2=3）完整保留；p1 用重启前的 `last_line_no=4` 领到第 5 行，
p2 重发旧请求 `last_line_no=2` 幂等返回第 3 行，稿尾仍正确报 `no_line`。

### 4. 并发测试

* 20 个线程栅栏同步、对同一台发完全相同的领词请求：恰好推进一次，
  20 个响应返回同一个新行号，无重复行；
* 34 轮、每轮 12 线程同发，从进度 26 一路领到第 60 行：每个行号恰好推进一次，
  无重复、无跳号、无丢失，稿尾正确报 `no_line`；
* 乱序票号并发时，落后于当前进度的请求按跳号 409 拒绝（`reason=gap`），符合规则。

## 遗留项 / 设计约束

1. **单 worker 约束**：原子性依赖进程内锁，水平扩容需改为多进程方案
   （如 PostgreSQL 行锁 `SELECT ... FOR UPDATE`，或为每台提词器建唯一进度行做原子 UPDATE）。
2. **重发与重复请求在协议上同形**：服务端只能凭 `last_line_no` 与当前进度的关系判断，
   无法区分网络重试与客户端重复点击；两者都按幂等返回当前行处理。客户端要领下一行时，
   应使用上次响应中返回的 `last_line_no` 作为新请求参数。
3. **无鉴权**：按题意假设内网可信环境，导演接口与提词器接口均未加认证；
   上生产前需补 API Key / 网络隔离。
4. **换稿为全量替换**：不支持 diff 式补丁，导演每次须提交整份稿件（题意如此）。
5. **本环境未安装 Docker**，`Dockerfile` 与 `docker-compose.yml` 已编写并通过 YAML/语法校验，
   但容器化启动未在本机实际执行；所有功能测试均在本机 uvicorn + SQLite 上完成。
6. 测试依赖（pytest/httpx）未写入 `requirements.txt`，运行时镜像只含生产依赖。
