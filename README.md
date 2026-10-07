# 园区访客通行规划服务（Park Route Planner）

面向园区访客的**离线**通行规划 HTTP 服务。导入一张带方向、通行秒数、台阶标记
与（可选）**通行容量**的步道图，以及按**半开时间区间** `[start, end)` 生效的
封闭记录；给定起终点、出发时刻与是否避台阶，返回**最早到达**路线及逐段的进入、
离开时间。路段还可携带**反向通行时间窗**数组：窗内仅允许从原终点走向原起点，
窗外仅允许原方向，方向按进入路段的时刻判定，封闭记录对两个方向同样全程生效。
支持在查询中指定**按顺序停靠的站点**（每站带开始停留时间窗与停留秒数），
见第 4.1 节；也支持为**多名访客**在候选节点中选择公共汇合点，见第 4.2 节。
高峰时段可通过**容量确认**接口为正整数人数的访客团原子锁定有容量保障的行程
（不足时在节点等待、并发不超额、可按编号查询与取消），见第 4.5 节；已确认
且尚未出发的行程可通过**改签**接口在当前版本与世代下排除自身原占用后重新
规划并一次性替换（修订号递增、起点不变），见第 4.6 节。确认
寻路**按需读取**实际触及的有容量路段占用，未触及路段上的行程增长不会使
单次确认的读取量线性增长，验证方式见第 5.1 节。
导入前可用**联合预检**接口一次性校验候选步道图与封闭记录（含反向通行窗与
容量字段），返回全部可判定错误且不改动当前数据，见第 4.3 节。误发布后可用
**版本恢复**把任一历史版本的节点、路段、封闭记录与反向通行窗整体恢复为新的
生效版本（版本号继续递增，历史仍可追溯），见第 4.4 节。

- 语言/存储：Python 3.11 + SQLite（标准库，无需外部数据库）
- Web 框架：FastAPI + Uvicorn
- 无外部网络依赖，适合园区内网 / 边缘设备离线运行

---

## 1. 快速启动

### 方式 A：Docker Compose（推荐，一键启动）

```bash
docker compose up -d --build
# 查看状态
curl http://localhost:8080/health
```

容器启动时会自动建库并导入内置示例数据（`app/sample_data.py`），随后在
`8080` 端口提供服务。数据持久化在命名卷 `park-route-data` 中。

停止：

```bash
docker compose down            # 保留数据
docker compose down -v         # 连同数据卷一起删除
```

### 方式 B：本地 Python 运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 建库 + 初始化数据（默认内置示例；--graph/--closures 可指定自定义 JSON）
python -m app.cli init-db

# 启动服务
python -m app.cli serve
# 或：uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> 直接用 `uvicorn` 启动时若库为空，服务可正常运行，只是查询会返回 `503 no_data`，
> 请改用导入接口写入数据，或使用 `python -m app.cli serve` / Docker 入口（会自动种子化）。

**服务地址与端口**：默认 `http://0.0.0.0:8080`，可用 `APP_HOST` / `APP_PORT` 修改。
交互式 API 文档：`http://localhost:8080/docs`。

---

## 2. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `APP_PORT` | `8080` | HTTP 监听端口 |
| `APP_DB_PATH` | `./data/routing.db`（容器内 `/data/routing.db`） | SQLite 数据库文件路径 |
| `APP_SEED_GRAPH` | （空） | 首次初始化使用的步道图 JSON 文件路径 |
| `APP_SEED_CLOSURES` | （空） | 首次初始化使用的封闭记录 JSON 文件路径 |
| `APP_SEED_SAMPLE` | `1` | 未指定种子文件时，是否导入内置示例数据；`0`/`false` 表示空库启动 |

---

## 3. 数据模型

### 步道图

- **节点**：字符串标识，如 `"GATE"`、`"PLAZA"`。
- **路段（边）**：有方向；字段：
  - `id`：路段编号，**同一版本内唯一**，用于平局字典序比较；
  - `from` / `to`：起终点，必须引用已声明的节点；
  - `travel_seconds`：通行秒数，**正整数**；
  - `has_stairs`：是否含台阶（布尔）；
  - `capacity`：**可选**，正整数通行容量，缺省或为 `null` 时表示**不限**。
    容量按“同一时刻在该路段上的人数”计量：已确认行程（见 4.5 节）按每段
    **实际进入至离开**的半开区间占用相应人数，同一半开区间内人数之和不得
    超过容量；容量不足时行程规划会在**节点等待**前段行程离开。容量对路段
    的两个通行方向同样生效（不分方向）；
  - `reverse_windows`：**可选**，反向通行时间窗数组，缺省或为空
    （`[]`/`null`）时行为不变。每项为：

    ```json
    {"start": "2026-10-05T11:00:00Z", "end": "2026-10-05T13:00:00Z"}
    ```

    时间格式与封闭记录一致（ISO 8601 或纪元秒），区间为**半开**
    `[start, end)`，必须 `start < end`；同一数组内各窗**不得重叠**
    （端点相接，如 `[a,b)` 与 `[b,c)`，允许）。

示例见 [`data/graph.example.json`](data/graph.example.json)；
带反向窗的示例见
[`data/graph.reverse-windows.example.json`](data/graph.reverse-windows.example.json)。

#### 反向通行语义

- **窗内只允许反向**：进入时刻落在某个 `[start, end)` 内时，该路段仅可
  从原终点 `to` 走向原起点 `from`；**窗外只允许原方向**；
- 方向以**进入路段的时刻**判定；进入后通行途中跨过窗边界（开始或结束）
  仍按进入时确定的方向走完全程，不会在路中"变回"；
- 允许在节点等待：窗外可等到窗开放再反向进入；窗内原方向则须等到
  窗结束；就绪时刻之后再无可用窗的方向不可达；
- **封闭记录对两个方向同样生效**：整个通行区间
  `[enter, enter + travel_seconds)` 不得与该边任何封闭区间重叠
  （半开判定不变），可在节点等待封闭结束——等待封闭可能越过窗边界，
  系统会重新判定可用方向；
- 反向通行同样遵守避台阶、最早到达、全程编号序列平局等既有规则。

### 封闭记录

- 作用于某条边，时间区间为**半开** `[start, end)`：
  - `end` 时刻该路段**立即恢复**，允许恰好在 `end` 进入；
  - `start` 时刻该路段已经封闭。
- 必须满足 `start < end`，时间使用 ISO 8601（推荐 `...Z`）；不带时区按 UTC 处理。

示例见 [`data/closures.example.json`](data/closures.example.json)。封闭记录为**整体替换**。

### 通行与等候规则

- 允许在节点等候封闭结束，或等候反向时间窗开放/结束；
- 一条边的整个通行区间 `[enter, enter + travel_seconds)` 不得与该边任何封闭区间重叠，
  不允许“先走进路段、在路中间等”；
- 寻路时自动计算每段实际进入时刻与等候秒数；
- 反向窗的方向判定与等待规则见上文“反向通行语义”。

### 容量占用规则（行程确认）

- 只有 `POST /api/trips`（4.5 节）的已确认行程占用容量；普通路线查询
  `POST /api/route` 与多人汇合 `POST /api/meeting` **不读取也不消耗容量**，
  语义与此前完全一致；
- 已确认行程按每段实际进入至离开的**半开区间** `[enter, leave)` 占用
  `people` 人容量：恰好在前段行程 `leave` 时刻进入不算超额（首尾相接允许）；
- 容量约束与封闭记录同口径：整段通行区间内任何时刻
  “既有占用人数 + 本次人数”不得超过容量，否则在节点等待至可行时刻；
- 容量不足时系统自动在节点等待（与封闭、反向窗等待共用同一套 FIFO
  时间依赖规则——晚到不会更早通过）；若等待后仍无可行行程（例如人数
  本身超过沿途所有路径的容量），请求被明确拒绝且**不留下任何占用**；
- 容量按**图发布世代**计量：封闭记录更新沿用当前世代（未取消行程继续
  计入），重新发布步道图或版本恢复开启**新世代独立计量**，旧行程仍可
  查询与取消。

---

## 4. HTTP API

所有请求/响应均为 JSON。

### `POST /api/route` — 查询路线

请求：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "departure_time": "2026-10-05T09:55:00Z",
  "avoid_stairs": false
}
```

响应 `200`（节选）：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "query_departure_time": "2026-10-05T09:55:00Z",
  "arrival_time": "2026-10-05T10:07:00Z",
  "total_waiting_seconds": 0,
  "avoid_stairs": false,
  "edge_sequence": ["E1", "E3", "E5"],
  "segments": [
    {
      "edge_id": "E1",
      "from_node": "GATE",
      "to_node": "PLAZA",
      "has_stairs": false,
      "enter_time": "2026-10-05T09:55:00Z",
      "leave_time": "2026-10-05T09:58:00Z",
      "travel_seconds": 180,
      "waiting_seconds": 0
    }
  ],
  "data_version": 2
}
```

- 每段都给出**进入**（`enter_time`，已含节点等候）与**离开**（`leave_time`）时刻，
  以及 `waiting_seconds`；
- 路段带反向时间窗时，`segments[]` 的 `from_node`/`to_node` 显示该段的
  **实际通行方向**（反向段即原终点→原起点）；`edge_id` 仍是同一路段编号，
  `edge_sequence` 平局规则也仍按路段编号。响应字段不新增、不删减，
  无反向窗数据时结构与旧版完全一致；
- `data_version` 标明本次查询实际使用的数据版本。

错误：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 起终点未知、时间格式错误、停靠点参数非法等 | `400` | `invalid_payload` |
| 无可行路线（含避台阶后不可达、任一停靠站无法按时停留） | `404` | `no_route` |
| 尚未导入步道图 | `503` | `no_data` |

### 4.1 按顺序停靠（`stops`，可选）

查询可携带 `stops` 数组，访客将**按给定顺序**完成各站停靠：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "departure_time": "2026-10-05T08:50:00Z",
  "avoid_stairs": false,
  "stops": [
    {
      "node": "LAKE",
      "earliest_start": "2026-10-05T09:00:00Z",
      "latest_start": "2026-10-05T09:10:00Z",
      "stay_seconds": 300
    }
  ]
}
```

每站字段：

| 字段 | 说明 |
| --- | --- |
| `node` | 停靠地点（必须是已知节点） |
| `earliest_start` | 最早开始停留时刻（ISO 8601 或纪元秒） |
| `latest_start` | 最晚开始停留时刻，须 `>= earliest_start`（闭区间） |
| `stay_seconds` | 停留秒数，**非负整数**（`0` 表示即停即走） |

语义规则：

- 抵达某站后可在节点**等到窗口开放**，开始停留时刻为
  `max(抵达时刻, earliest_start)`，且**不得晚于 `latest_start`**，
  否则本次查询无路线（`404 no_route`）；
- **结束停留后才能前往下一站**；途经尚未轮到的站点（包括未来各站所在
  节点）**不算完成停靠**，只当作普通节点通过；
- 起点本身就是第 1 站、或相邻两站位于同一节点时，立即（链式）完成停靠；
- 选路目标不变：**最终到达**（完成全部停靠并抵达终点）最早优先，平局按
  **全程**路段编号序列字典序选取，不逐段独立打破平局；
- 封闭区间（半开）、避台阶、单次查询固定数据版本的规则对每一程同样生效。

带停靠时响应在原有结构基础上增加 `stops` 明细与 `total_stay_seconds`
（无停靠点时不出现这两个字段，返回结构与之前完全一致）：

```json
{
  "arrival_time": "2026-10-05T09:09:00Z",
  "total_waiting_seconds": 120,
  "total_stay_seconds": 300,
  "edge_sequence": ["E1", "E3", "E5"],
  "segments": ["... 全程逐段（跨各程拼接） ..."],
  "stops": [
    {
      "node": "LAKE",
      "arrival_time": "2026-10-05T08:58:00Z",
      "start_time": "2026-10-05T09:00:00Z",
      "end_time": "2026-10-05T09:05:00Z",
      "stay_seconds": 300,
      "wait_for_window_seconds": 120
    }
  ],
  "data_version": 2
}
```

- `stops[i]` 给出第 `i+1` 站的**抵达**、**开始停留**、**结束停留**时刻
  （均附 `_epoch` 秒级副本）与等窗秒数；
- `total_waiting_seconds` 为非通行、非停留的等候总秒数（封闭等候 + 等窗）；
- `segments` 覆盖**全程**（各程拼接），`waiting_seconds` 仍为进入该路段前
  在节点的封闭等候。

错误映射：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 时间窗无效（`earliest_start > latest_start`、时间格式错误） | `400` | `invalid_payload` |
| `stay_seconds` 非非负整数（负数、小数、布尔、字符串） | `400` | `invalid_payload` |
| 停靠地点未知 | `400` | `invalid_payload` |
| 任一站无法按时停留（抵达已晚于 `latest_start`） | `404` | `no_route` |
| 某站或终点不可达 | `404` | `no_route` |

### 4.2 多人汇合点（`POST /api/meeting`）

一次请求给出**至少两名**访客（各自的起点、最早出发时刻、避台阶标记）、
**非空**候选节点列表和共同的**最晚汇合时刻**，服务在候选点中选出公共汇合点：

```json
{
  "visitors": [
    {"id": "alice", "origin": "GATE",
     "departure_time": "2026-10-05T09:55:00Z", "avoid_stairs": false},
    {"id": "bob", "origin": "PLAZA",
     "departure_time": "2026-10-05T09:50:00Z", "avoid_stairs": false}
  ],
  "candidates": ["TOWER", "LAKE"],
  "latest_meeting_time": "2026-10-05T12:00:00Z"
}
```

访客字段：

| 字段 | 说明 |
| --- | --- |
| `origin` | 起点（必须是已知节点） |
| `departure_time` | **最早出发时刻**（ISO 8601 或纪元秒）；按既有封闭、等候与最早到达规则独立通行 |
| `avoid_stairs` | 是否避台阶（布尔，默认 `false`） |
| `id` | 可选访客标识（字符串/整数），缺省按 1 起始序号；同一请求内不得重复 |

语义规则：

- 每名访客从自己的最早出发时刻**独立**通行，规则与 `POST /api/route` 完全一致
  （半开封闭区间、节点等候、避台阶、最早到达 + 全程路段编号序列字典序破平局）；
- 抵达汇合点后可在该节点等待其余访客；
- 只接受**所有人**的抵达时刻都**不晚于** `latest_meeting_time` 的候选点
  （恰好在最晚时刻抵达也可以）；
- 合格候选依次按以下标准择优：
  1. **全员实际汇合时刻**（最晚个人抵达时刻）最早；
  2. **个人抵达时刻总和**最小；
  3. **候选节点编号字典序**最小（字符串比较，如 `"N10" < "N2"`）；
- 单人路线的平局仍按其**全程路段编号序列字典序**处理，与单人路线查询语义不变；
- 整次计算在请求开始时固定**同一数据版本**，候选评估与全部访客共用该版本。

响应 `200`：

```json
{
  "meeting_node": "LAKE",
  "meeting_time": "2026-10-05T10:03:00Z",
  "meeting_time_epoch": 1791194580,
  "latest_meeting_time": "2026-10-05T12:00:00Z",
  "latest_meeting_time_epoch": 1791201600,
  "total_arrival_seconds": 3582389100,
  "visitors": [
    {
      "id": "alice",
      "origin": "GATE",
      "destination": "LAKE",
      "query_departure_time": "2026-10-05T09:55:00Z",
      "arrival_time": "2026-10-05T10:03:00Z",
      "avoid_stairs": false,
      "edge_sequence": ["E1", "E3"],
      "segments": ["... 与单人路线查询结构一致的逐段明细 ..."],
      "wait_after_arrival_seconds": 0,
      "total_waiting_seconds": 0
    },
    {
      "id": "bob",
      "origin": "PLAZA",
      "destination": "LAKE",
      "arrival_time": "2026-10-05T09:55:00Z",
      "edge_sequence": ["E3"],
      "segments": ["..."],
      "wait_after_arrival_seconds": 480,
      "total_waiting_seconds": 0
    }
  ],
  "data_version": 2
}
```

- `meeting_time` 为全员实际汇合时刻（即最晚个人抵达时刻）；
- `visitors[]` 与请求中的访客顺序一致，每人给出完整单人路线（字段结构同
  `POST /api/route`，起点即候选点时 `edge_sequence`/`segments` 为空数组）；
- `wait_after_arrival_seconds` 为该访客**到达汇合点之后**等待全员到齐的秒数
  （`meeting_time - arrival_time`，最晚抵达者为 `0`）；
- `total_arrival_seconds` 为个人抵达时刻（纪元秒）总和，即二级择优指标；
- 所有时刻均附 `_epoch` 秒级副本。

错误映射：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 字段非法（访客少于两人、时间格式错误、`avoid_stairs` 非布尔等） | `400` | `invalid_payload` |
| 起点或候选节点未知、候选列表为空或重复、访客 `id` 重复 | `400` | `invalid_payload` |
| 没有任何候选点能让全员在最晚时刻前抵达（含某人不可达/避台阶后不可达） | `404` | `no_meeting_candidate` |
| 尚未导入步道图（空图） | `503` | `no_data` |

### 4.3 联合预检（`POST /admin/precheck`）

导入前对**候选步道图 + 候选封闭记录**做一次联合校验。校验规则与
`POST /admin/graph`、`POST /admin/closures` 完全一致（字段、时间区间、
节点与路段引用、反向窗重叠、**容量字段必须为正整数或 null**），区别
在于：

- 一次请求同时提交两类输入，封闭记录的路段引用按**本次提交的候选图**
  的路段编号校验（与库中当前版本无关）；
- 返回**全部**可判定错误及其在原数组中的位置，而不是只报第一个；
- 候选图自身无效时，封闭记录的路段引用不可判定——**跳过**引用校验
  （不误报），封闭记录自身的字段/时间区间错误仍照常报告；
- **只读**：无论结果如何都不改变当前数据版本。

请求：

```json
{
  "graph":    {"nodes": ["..."], "edges": ["... 同 /admin/graph 负载 ..."]},
  "closures": {"closures": ["... 同 /admin/closures 负载 ..."]}
}
```

响应（HTTP `200`）：

```json
{
  "ok": false,
  "errors": [
    {"source": "graph", "path": "edges[1].travel_seconds", "index": 1,
     "message": "通行秒数(travel_seconds)必须为正数，收到 0"},
    {"source": "closures", "path": "closures[0].edge_id", "index": 0,
     "message": "closures[0] 引用了不存在的路段: 'E9'"}
  ],
  "data_version": 2
}
```

- `source`：`graph` 或 `closures`，标明错误来自哪类输入；
- `path`：错误位置（相对该类输入的负载），方括号内即原数组下标；
- `index`：`path` 中最内层数组元素的下标，无对应数组元素时为 `null`；
- `message`：与导入接口一致的错误描述；
- `data_version`：当前生效的数据版本，预检不会改动它。

预检通过时返回候选数据的节点、路段、封闭记录与反向时间窗条数：

```json
{
  "ok": true,
  "errors": [],
  "counts": {"nodes": 5, "edges": 6, "closures": 1, "reverse_windows": 2},
  "data_version": 2
}
```

错误映射：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 候选数据未通过校验（响应体含全部错误明细） | `200` | —（`ok: false`） |
| 请求包络非法（非 JSON 对象、缺少 `graph`/`closures`） | `400` | `invalid_payload` |

### 4.4 版本恢复（`POST /admin/restore`）

管理员误发布步道图或封闭记录后，把某个**已存在的历史版本**的节点、
路段、封闭记录与反向通行窗**整体恢复为新的生效版本**：

```bash
curl -X POST http://localhost:8080/admin/restore \
  -H 'Content-Type: application/json' \
  -d '{"version": 2}'
```

响应 `200`：

```json
{
  "status": "restored",
  "source_version": 2,
  "data_version": 5,
  "previous_version": 4
}
```

- `source_version` 为被恢复的历史版本，`data_version` 为恢复生成的
  **新**生效版本，`previous_version` 为恢复前的生效版本；
- 恢复**不是回拨指针**：版本号继续递增，历史版本（含被覆盖的误发布
  版本）全部保留，仍可按版本号追溯；
- 恢复与导入（`/admin/graph`、`/admin/closures`）共用同一串行写事务：
  并发时按提交顺序生效，任一时刻的生效版本都是完整数据；路线查询在
  请求开始时固定版本指针，只会读到恢复前**或**恢复后的完整状态，
  不会读到拼接数据；
- 恢复会**开启新的图发布世代**：恢复后新确认的行程按新世代独立计量，
  旧世代行程不再占用新世代容量，但仍可按编号查询与取消；
- 恢复当前版本自身是允许的（生成一个内容相同的新版本）；
- 命令行等价用法：`python -m app.cli restore 2`。

错误映射：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 请求体非 JSON、缺 `version`、`version` 非正整数 | `400` | `invalid_payload` |
| 目标版本不存在 | `404` | `version_not_found` |

拒绝时响应体含 `"rejected": true` 与当前仍生效的 `active_version`，
生效版本保持不变。

### 4.5 高峰容量确认（`POST /api/trips` 等）

高峰时段为访客团确认一条**有容量保障**的行程：系统沿用封闭、反向通行窗、
避台阶与最早到达规则，额外按路段 `capacity` 校验容量，不足时在节点等待；
确认成功在一个**串行写事务**内原子写入行程（编号、逐段时刻、数据版本与
世代），并发确认绝不超额。规划在同一事务内**按需读取**占用：只有寻路
实际触及的**有容量**路段才触发一次按路段查询（经 `idx_trip_seg_edge`
索引定位，并过滤不晚于出发时刻即结束的占用），未触及路段上的行程增长
不影响单次确认的读取量；普通路线与汇合查询仍完全不读取容量占用。

#### `POST /api/trips` — 确认行程

请求（`people` 为必填的**正整数**人数；`data_version` 为可选的乐观版本号）：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "departure_time": "2026-10-05T09:00:00Z",
  "avoid_stairs": false,
  "people": 2,
  "data_version": 2
}
```

响应 `201`：

```json
{
  "trip_id": 7,
  "status": "confirmed",
  "origin": "GATE",
  "destination": "TOWER",
  "query_departure_time": "2026-10-05T09:00:00Z",
  "query_departure_time_epoch": 1791133200,
  "arrival_time": "2026-10-05T09:09:00Z",
  "arrival_time_epoch": 1791133740,
  "avoid_stairs": false,
  "people": 2,
  "edge_sequence": ["E1", "E3", "E5"],
  "segments": [
    {
      "edge_id": "E1",
      "from_node": "GATE",
      "to_node": "PLAZA",
      "has_stairs": false,
      "enter_time": "2026-10-05T09:00:00Z",
      "enter_time_epoch": 1791133200,
      "leave_time": "2026-10-05T09:03:00Z",
      "leave_time_epoch": 1791133380,
      "travel_seconds": 180,
      "waiting_seconds": 0,
      "capacity": 2
    }
  ],
  "data_version": 2,
  "generation_id": 1,
  "created_at_epoch": 1791133200
}
```

- 逐段结构与 `/api/route` 一致（含实际通行方向、等候秒数），额外返回每段
  确认时的 `capacity`（缺省不限为 `null`）；
- `waiting_seconds` 同时涵盖封闭等待、反向窗等待与**容量等待**；
- `data_version` 为本次规划实际使用的数据版本，`generation_id` 为容量
  计量所属的图发布世代；
- 起点即终点（零段行程）允许确认，`segments`/`edge_sequence` 为空数组。

**数据版本处理**：携带 `data_version` 时，若请求进入写事务后发现当前版本
已变化，系统**不会基于旧版本写入**，返回 `409 version_conflict`（响应体给出
`expected_version` 与 `current_version`），由调用方基于新版本重新规划后再
确认；不携带则总是按当前版本规划。无论哪种情况都不会写入过时方案。

错误：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 字段非法（缺起终点/出发时刻、未知节点、时间格式错误、`people` 非正整数、`data_version` 非正整数、携带 `stops` 等） | `400` | `invalid_payload` |
| 封闭/反向窗/容量约束下无可行行程（含人数超过沿途所有路径容量、避台阶后不可达）；**不留下占用** | `404` | `no_route` |
| 携带的 `data_version` 已过期；**不写入** | `409` | `version_conflict` |
| 尚未导入步道图 | `503` | `no_data` |

#### `GET /api/trips/{trip_id}` — 按编号查询

返回确认时的完整行程（结构同上，另附当前修订号 `revision_no`；
已取消行程附 `cancelled_at`/`cancelled_at_epoch`）。每次成功改签后
修订号递增（初始为 `1`），可作为下次改签的乐观并发凭据。重新发布
步道图或恢复历史版本后，旧世代行程**仍可查询**。编号不存在返回
`404 trip_not_found`；编号非正整数返回 `400 invalid_payload`。

#### `POST /api/trips/{trip_id}/cancel` — 取消行程

在写事务内把行程置为 `cancelled` 并**一次性释放**其在所属世代的容量。
响应体为更新后的行程，另含布尔字段 `released`：

- 首次取消：`released: true`；
- **重复取消幂等**：`released: false`，不重复释放容量，行程保持已取消；
- 编号不存在：`404 trip_not_found`。

容量释放后，后续同时间窗的确认可立即使用相应容量（见第 5 节示例）。

#### `POST /api/trips/{trip_id}/reschedule` — 改签已确认行程

访客团可对**已确认且尚未出发**的行程改签：起点与行程编号保持不变，
可改终点、出发时刻、人数与避台阶选项。改签与确认、取消、数据发布
共用同一个 `BEGIN IMMEDIATE` 串行写事务：在**当前数据版本与图发布
世代**下重新规划，**先排除本行程原占用**，再按既有封闭、反向窗与
半开容量规则确认新路线；成功后**一次性替换**逐段时刻、人数、
`data_version` 与 `generation_id`，**修订号 `revision_no` 递增 1**，
返回替换后的完整行程。任何拒绝都不改变原行程及其容量占用。

请求（`revision_no` 为**当前修订号**，必填正整数；旧库行程的初始
修订号为 `1`；`data_version` 为可选的乐观版本号）：

```json
{
  "revision_no": 1,
  "departure_time": "2026-10-08T09:00:00Z",
  "destination": "TOWER",
  "people": 3,
  "avoid_stairs": false,
  "data_version": 2
}
```

- `departure_time` 为**新出发时刻**，必须**严格晚于服务端当前时刻**
  （半开：与当前时刻相同即视为已出发/非法）；格式同确认接口；
- `destination`、`people`（正整数）、`avoid_stairs` 语义与确认一致；
  请求携带 `origin` 或 `stops` 一律视为非法（起点不可改、改签不支持
  停靠）；请求体内也可给 `trip_id`，但必须与路径编号一致；
- 重新规划在请求开始时固定的当前版本/世代上进行：封闭更新后改签
  沿用同世代（其他未取消行程继续计量），步道图重新发布或版本恢复
  后改签则按新世代独立计量——改签成功的行程整体迁入新版本/新世代，
  旧世代不再统计它；
- “先排除本行程原占用”意味着新路线不会被该行程自己的旧逐段占用
  阻挡，也不会被重复计入容量；**其他**同世代未取消行程照常计量，
  容量不足时仍在节点等待；
- 响应 `200` 为替换后的完整行程（结构与确认/查询一致，含逐段
  `capacity`、`data_version`、`generation_id` 与已递增的
  `revision_no`）。

错误：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 编号非正整数、请求体非法（缺字段、人数非正整数、修订号非正整数、时间格式错误、携带 `origin`/`stops`、未知终点等） | `400` | `invalid_payload` |
| 新出发时刻不晚于当前时刻 | `400` | `invalid_payload` |
| 行程不存在 | `404` | `trip_not_found` |
| 当前版本/世代下新路线不可行（容量/封闭/反向窗/避台阶）；原行程与占用不变 | `404` | `no_route` |
| 修订号与库内当前修订号不一致（并发改签/取消）；不写入 | `409` | `revision_conflict` |
| 携带的 `data_version` 已过期；不写入 | `409` | `version_conflict` |
| 行程已取消或已出发；原行程及占用不变 | `409` | `trip_not_modifiable` |
| 尚未导入步道图 | `503` | `no_data` |

`revision_conflict` 响应体给出 `expected_revision` 与
`current_revision`；`version_conflict` 给出 `expected_version` 与
`current_version`。调用方应重新 `GET /api/trips/{id}` 取最新行程与
版本后再改签。`GET /api/trips/{trip_id}` 的响应同步包含
`revision_no`（新增字段，其余字段与行为不变；取消响应亦保留它）。

### `POST /admin/graph` — 原子发布新步道图

```bash
curl -X POST http://localhost:8080/admin/graph \
  -H 'Content-Type: application/json' \
  -d @data/graph.example.json
```

校验失败（未知节点引用、非正通行秒数、编号重复、**容量字段非正整数**、
反向时间窗无效或同一路段上时间窗重叠等）返回 `400`，响应体含
`"rejected": true` 与当前仍生效的 `active_version`，**旧版本完整保留**
（包括旧图的节点、路段、封闭记录、容量与反向窗）。发布成功后：

- 旧封闭记录中引用了已删除路段的条目会自动清理；
- 反向窗随新图**整体替换**（新图未携带即清空）；
- 容量字段随新图整体替换；本次发布**开启新的图发布世代**——新确认行程
  按新世代独立计量容量，此前已确认（未取消）行程不再计入新世代，也不
  阻挡新世代确认，但仍可按编号查询与取消。

带反向窗的图导入：

```bash
curl -X POST http://localhost:8080/admin/graph \
  -H 'Content-Type: application/json' \
  -d @data/graph.reverse-windows.example.json
```

### `POST /admin/closures` — 原子整体替换封闭记录

```bash
curl -X POST http://localhost:8080/admin/closures \
  -H 'Content-Type: application/json' \
  -d @data/closures.example.json
```

引用不存在的路段、`start >= end` 等返回 `400` 并保留旧版本。封闭记录
更新**不开启新图发布世代**：版本号递增但世代号不变，该世代内已确认
（未取消）行程继续占用容量，新确认与之一并计量。

### 其他

- `GET /health`：健康检查与当前版本号；
- `GET /version`：当前版本、**图发布世代**（`generation_id`）及节点/路段/
  封闭/反向窗数量（`reverse_windows` 为反向时间窗总条数）。

---

## 5. 调用示例

```bash
# 09:55 出发（E4 在 10:00–11:00 封闭），不避台阶 → 绕行平路
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T09:55:00Z","avoid_stairs":false}'

# 避台阶：只经过 has_stairs=false 的路段
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T08:00:00Z","avoid_stairs":true}'

# 节点等候：HILL 09:59 出发，E4 [10:00,11:00) 封 → 在 HILL 等到 11:00 进入
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"HILL","destination":"TOWER",
       "departure_time":"2026-10-05T09:59:00Z","avoid_stairs":false}'

# 按顺序停靠：08:50 出发，LAKE 站 09:00–09:10 之间开始停留、停留 300 秒
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T08:50:00Z",
       "stops":[{"node":"LAKE",
                 "earliest_start":"2026-10-05T09:00:00Z",
                 "latest_start":"2026-10-05T09:10:00Z",
                 "stay_seconds":300}]}'
# 结果要点：E1(08:50→08:53) → E3(08:53→08:58) 抵 LAKE；
#   等窗 120 秒，09:00 开始停留，09:05 结束；E5(09:05→09:09)；
#   arrival_time=2026-10-05T09:09:00Z，stops[0] 记录 08:58/09:00/09:05。

# 停靠窗口不可行：最晚 08:55 开始，但最早 08:58 才到 LAKE → 404 no_route
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T08:50:00Z",
       "stops":[{"node":"LAKE",
                 "earliest_start":"2026-10-05T08:00:00Z",
                 "latest_start":"2026-10-05T08:55:00Z",
                 "stay_seconds":60}]}'

# 导入带反向时间窗的步道图（E5 在 11:00–13:00、14:00–16:00 反向）
curl -s -X POST http://localhost:8080/admin/graph \
  -H 'Content-Type: application/json' \
  -d @data/graph.reverse-windows.example.json

# 窗内反向：12:00 从 TOWER 进入 E5（原方向 LAKE->TOWER），12:04 到 LAKE；
# 逐段 from_node/to_node 显示实际方向 TOWER -> LAKE
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"TOWER","destination":"LAKE",
       "departure_time":"2026-10-05T12:00:00Z","avoid_stairs":false}'

# 窗外允许在节点等窗：09:00 出发 -> 等到 11:00 开窗再反向进入
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"TOWER","destination":"LAKE",
       "departure_time":"2026-10-05T09:00:00Z","avoid_stairs":false}'

# 方向按进入时刻判定：12:58 反向进入 E5（240 秒），
# 途中 13:00 窗结束，仍按反向走完，13:02 抵达 LAKE
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"TOWER","destination":"LAKE",
       "departure_time":"2026-10-05T12:58:00Z","avoid_stairs":false}'

# 16:30 出发：之后再无反向窗，TOWER -> LAKE 不可达 → 404 no_route
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"TOWER","destination":"LAKE",
       "departure_time":"2026-10-05T16:30:00Z","avoid_stairs":false}'

# 窗内原方向禁行：LAKE 12:00 出发须等到 13:00 窗结束才能走 E5 → 13:04 到
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"LAKE","destination":"TOWER",
       "departure_time":"2026-10-05T12:00:00Z","avoid_stairs":false}'

# 反向窗无效（start==end）或重叠 → 400 rejected，active_version 保持不变
curl -s -X POST http://localhost:8080/admin/graph \
  -H 'Content-Type: application/json' \
  -d '{"nodes":["A","B"],"edges":[
       {"id":"E1","from":"A","to":"B","travel_seconds":100,"has_stairs":false,
        "reverse_windows":[
          {"start":"2026-10-05T11:00:00Z","end":"2026-10-05T12:30:00Z"},
          {"start":"2026-10-05T12:00:00Z","end":"2026-10-05T13:00:00Z"}]}]}'

# 联合预检：候选图 + 封闭记录一次校验（只读，不改动当前版本）
curl -s -X POST http://localhost:8080/admin/precheck \
  -H 'Content-Type: application/json' \
  -d '{"graph":{"nodes":["A","B"],"edges":[
         {"id":"E1","from":"A","to":"B","travel_seconds":60,"has_stairs":false,
          "reverse_windows":[
            {"start":"2026-10-05T11:00:00Z","end":"2026-10-05T12:00:00Z"}]}]},
       "closures":{"closures":[
         {"edge_id":"E1","start":"2026-10-05T08:00:00Z","end":"2026-10-05T09:00:00Z"}]}}'
# → {"ok":true,"errors":[],
#    "counts":{"nodes":2,"edges":1,"closures":1,"reverse_windows":1},
#    "data_version":...}

# 预检失败：一次返回全部可判定错误及原数组位置；
# 图无效时 E9 的路段引用不误报，但封闭项自身 start>=end 的错误仍报告
curl -s -X POST http://localhost:8080/admin/precheck \
  -H 'Content-Type: application/json' \
  -d '{"graph":{"nodes":["A"],"edges":[
         {"id":"E1","from":"A","to":"GHOST","travel_seconds":0,"has_stairs":false}]},
       "closures":{"closures":[
         {"edge_id":"E9","start":"2026-10-05T09:00:00Z","end":"2026-10-05T08:00:00Z"}]}}'
# → {"ok":false,"errors":[
#      {"source":"graph","path":"edges[0].to","index":0,"message":"...未定义的节点: GHOST"},
#      {"source":"graph","path":"edges[0].travel_seconds","index":0,"message":"...必须为正数..."},
#      {"source":"closures","path":"closures[0]","index":0,"message":"...无效时间区间..."}],
#    "data_version":...}

# 版本恢复：把历史版本 v2 的节点/路段/封闭记录/反向窗整体恢复为新的生效版本
curl -s -X POST http://localhost:8080/admin/restore \
  -H 'Content-Type: application/json' \
  -d '{"version": 2}'
# → {"status":"restored","source_version":2,"data_version":5,"previous_version":4}

# 目标版本不存在 → 404 version_not_found，active_version 保持不变
curl -s -X POST http://localhost:8080/admin/restore \
  -H 'Content-Type: application/json' \
  -d '{"version": 999}'
# → {"error":"version_not_found","detail":"版本 v999 不存在，无法恢复",
#    "rejected":true,"active_version":5}

# 参数非法（version 非正整数）→ 400 invalid_payload，active_version 保持不变
curl -s -X POST http://localhost:8080/admin/restore \
  -H 'Content-Type: application/json' \
  -d '{"version": "2"}'

# 冒烟自检（需服务已启动；内含反向窗导入/拒绝、窗内通行、联合预检与版本恢复检查）
python3 scripts/smoke_test.py http://localhost:8080
```

容量确认（高峰行程）示例（示例图 E1/E3/E5 容量为 2 人）：

```bash
# 确认 2 人行程：容量充足时立即写入，返回编号、逐段时刻与数据版本
curl -s -X POST http://localhost:8080/api/trips \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T09:00:00Z",
       "avoid_stairs":false,"people":2}'
# -> 201 {"trip_id":1,"status":"confirmed","data_version":2,"generation_id":1,
#         "edge_sequence":["E1","E3","E5"],"segments":[...每段 capacity ...]}

# 同一时刻再确认一单 2 人：E1 容量 2 已被占用 → 在 GATE 等到 09:03 进入
curl -s -X POST http://localhost:8080/api/trips \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T09:00:00Z","people":2}'
# -> 201 第一段 enter_time=09:03:00（waiting_seconds=180）

# 人数本身超过容量（3 > 2，避台阶后无其他平路可选）→ 404，不留占用
curl -s -X POST http://localhost:8080/api/trips \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T09:00:00Z",
       "avoid_stairs":true,"people":3}'
# -> 404 {"error":"no_route", ...}

# 携带过期数据版本 → 409 冲突，不写入；取 current_version 后重新确认
curl -s -X POST http://localhost:8080/api/trips \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T09:00:00Z","people":1,
       "data_version":1}'
# -> 409 {"error":"version_conflict",
#         "expected_version":1,"current_version":2}

# 按编号查询
curl -s http://localhost:8080/api/trips/1

# 取消（首次 released=true，容量立即归还该世代）
curl -s -X POST http://localhost:8080/api/trips/1/cancel
# 重复取消幂等：released=false，不重复释放
curl -s -X POST http://localhost:8080/api/trips/1/cancel

# 改签已确认且尚未出发的行程（修订号从查询结果取，初始为 1）
# 起点与编号不变；改终点/出发时刻/人数/避台阶，修订号 1 -> 2
curl -s -X POST http://localhost:8080/api/trips/7/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"revision_no":1,
       "departure_time":"2026-10-08T09:30:00Z",
       "destination":"LAKE","people":3,"avoid_stairs":false}'
# -> 200 {"trip_id":7,"origin":"GATE","destination":"LAKE",
#         "people":3,"revision_no":2,
#         "data_version":...,"generation_id":...,"segments":[...新逐段...]}

# 修订号过期（已被并发改签）→ 409 revision_conflict，不写入
curl -s -X POST http://localhost:8080/api/trips/7/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"revision_no":1,
       "departure_time":"2026-10-09T09:00:00Z",
       "destination":"TOWER","people":2}'
# -> 409 {"error":"revision_conflict",
#         "expected_revision":1,"current_revision":2}

# 新出发时刻已过去 / 试图修改起点 → 400 invalid_payload
curl -s -X POST http://localhost:8080/api/trips/7/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"revision_no":2,"origin":"PLAZA",
       "departure_time":"2000-01-01T00:00:00Z",
       "destination":"TOWER","people":2}'

# 当前版本/世代下新路线不可行（人数超过沿途容量等）→ 404 no_route，
# 原行程与原占用均不变，修订号也不递增
curl -s -X POST http://localhost:8080/api/trips/7/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"revision_no":2,
       "departure_time":"2026-10-09T09:00:00Z",
       "destination":"TOWER","people":99}'

# 当前版本与图发布世代
curl -s http://localhost:8080/version
# -> {"data_version":2,"generation_id":1,"nodes":5,"edges":6,...}
```

```python
import json, urllib.error, urllib.request

def book(payload):
    req = urllib.request.Request(
        "http://localhost:8080/api/trips",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)

payload = {
    "origin": "GATE", "destination": "TOWER",
    "departure_time": "2026-10-05T09:00:00Z",
    "avoid_stairs": False, "people": 2,
}
status, trip = book(payload)
if status == 201:
    print("已确认行程", trip["trip_id"], "到达", trip["arrival_time"],
          "数据版本", trip["data_version"], "世代", trip["generation_id"])
elif status == 409:
    # 数据版本已变化：基于新版本重新规划后再确认
    payload["data_version"] = trip["current_version"]
    status, trip = book(payload)
elif status == 404:
    print("无可行行程（容量/封闭/窗约束），未产生任何占用")
```

改签（修订号乐观并发）示例：

```python
import json, urllib.error, urllib.request

def reschedule(trip_id, revision, payload):
    body = {"revision_no": revision, **payload}
    req = urllib.request.Request(
        f"http://localhost:8080/api/trips/{trip_id}/reschedule",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)

status, trip = reschedule(7, 1, {
    "departure_time": "2026-10-08T09:30:00Z",
    "destination": "LAKE", "people": 3, "avoid_stairs": False,
})
if status == 200:
    print("改签成功，新修订号", trip["revision_no"], "到达", trip["arrival_time"])
elif status == 409 and trip["error"] == "revision_conflict":
    # 并发改签/取消使修订号变化：重新查询后用 current_revision 重试
    _, latest = reschedule(
        7, trip["current_revision"],
        {"departure_time": "2026-10-08T09:30:00Z",
         "destination": "LAKE", "people": 3},
    )
    print("按最新修订号改签：", latest["revision_no"])
else:
    print("改签被拒绝（原行程不变）：", status, trip["error"])
```

多人汇合点示例：

```bash
# alice 09:55 从 GATE、bob 09:50 从 PLAZA 出发；
# LAKE 汇合 10:03（alice 10:03 到、bob 09:55 到，bob 等 480 秒），
# TOWER 汇合 10:07 → 汇合时刻最早选 LAKE
curl -s -X POST http://localhost:8080/api/meeting \
  -H 'Content-Type: application/json' \
  -d '{"visitors":[
         {"id":"alice","origin":"GATE",
          "departure_time":"2026-10-05T09:55:00Z","avoid_stairs":false},
         {"id":"bob","origin":"PLAZA",
          "departure_time":"2026-10-05T09:50:00Z","avoid_stairs":false}],
       "candidates":["TOWER","LAKE"],
       "latest_meeting_time":"2026-10-05T12:00:00Z"}'

# 无人能在最晚时刻前抵达所有候选 → 404 no_meeting_candidate
curl -s -X POST http://localhost:8080/api/meeting \
  -H 'Content-Type: application/json' \
  -d '{"visitors":[
         {"origin":"GATE","departure_time":"2026-10-05T09:55:00Z"},
         {"origin":"PLAZA","departure_time":"2026-10-05T09:50:00Z"}],
       "candidates":["LAKE"],
       "latest_meeting_time":"2026-10-05T10:00:00Z"}'
```

```python
import json, urllib.request

req = urllib.request.Request(
    "http://localhost:8080/api/meeting",
    data=json.dumps({
        "visitors": [
            {"id": "alice", "origin": "GATE",
             "departure_time": "2026-10-05T09:55:00Z", "avoid_stairs": False},
            {"id": "bob", "origin": "PLAZA",
             "departure_time": "2026-10-05T09:50:00Z", "avoid_stairs": False},
        ],
        "candidates": ["TOWER", "LAKE"],
        "latest_meeting_time": "2026-10-05T12:00:00Z",
    }).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    plan = json.load(resp)
print(plan["meeting_node"], plan["meeting_time"])
for v in plan["visitors"]:
    print(v["id"], v["arrival_time"], v["edge_sequence"],
          "wait_after_arrival", v["wait_after_arrival_seconds"])
```

Python 示例：

```python
import json, urllib.request

req = urllib.request.Request(
    "http://localhost:8080/api/route",
    data=json.dumps({
        "origin": "GATE",
        "destination": "TOWER",
        "departure_time": "2026-10-05T09:55:00Z",
        "avoid_stairs": True,
    }).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    route = json.load(resp)
print(route["arrival_time"], route["edge_sequence"])
```

联合预检 Python 示例：

```python
import json, urllib.request

req = urllib.request.Request(
    "http://localhost:8080/admin/precheck",
    data=json.dumps({
        "graph": {
            "nodes": ["A", "B"],
            "edges": [
                {"id": "E1", "from": "A", "to": "B",
                 "travel_seconds": 60, "has_stairs": False,
                 "reverse_windows": [
                     {"start": "2026-10-05T11:00:00Z",
                      "end": "2026-10-05T12:00:00Z"}]},
            ],
        },
        "closures": {
            "closures": [
                {"edge_id": "E1",
                 "start": "2026-10-05T08:00:00Z",
                 "end": "2026-10-05T09:00:00Z"}],
        },
    }).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    report = json.load(resp)
if report["ok"]:
    print("预检通过:", report["counts"])
else:
    for err in report["errors"]:
        print(err["source"], err["path"], err["message"])
```

版本恢复 Python 示例：

```python
import json, urllib.request

req = urllib.request.Request(
    "http://localhost:8080/admin/restore",
    data=json.dumps({"version": 2}).encode(),  # 要恢复的历史版本号
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    result = json.load(resp)
print("来源版本:", result["source_version"],
      "新生效版本:", result["data_version"])
```

### 5.1 容量确认的占用读取性能验证（按需读取）

确认寻路**不预加载**当前图发布世代的全部占用，而是按需读取实际触及的
有容量路段占用：每条触及路段经 `idx_trip_seg_edge(edge_id, leave_ts)`
索引做一次范围查询（同时过滤 `leave_ts <= 出发时刻` 的占用——半开区间
端点相接本就不可能阻挡），结果按路段缓存，每条路段在一次确认内至多
读取一次；未触及路段上的行程增长不会使单次确认的读取量线性增长。

用基准脚本验证（仅用标准库，直接走存储层）：在步道 E9 上累积 K 条
已确认行程后，在互不相干的步道 E1 上做一次确认，统计本次确认实际
读取的占用行数：

```bash
python3 scripts/bench_occupancy.py             # 默认 K = 0 200 2000
python3 scripts/bench_occupancy.py 0 500 5000  # 自定义 K
```

输出示例：

```text
在 E9 上累积 K 条已确认行程后，在 E1 上做一次确认：
K=0      触及路段=['E1'] 按需读取=2 行 | 旧版预加载=3 行 | 本次确认耗时=0.4 ms
K=200    触及路段=['E1'] 按需读取=2 行 | 旧版预加载=203 行 | 本次确认耗时=0.3 ms
K=2000   触及路段=['E1'] 按需读取=2 行 | 旧版预加载=2003 行 | 本次确认耗时=0.3 ms
```

`按需读取` 列恒定（只读 E1 自身的 2 条占用），确认耗时不随 K 增长；
作为对照的旧版“预加载整个世代全部占用”的读取量随 K 线性增长。
`tests/test_on_demand.py` 同步固化该行为：未触及路段行程增长读量不变、
按需视图与全量预加载规划结果一致、占用查询走索引无整表扫描、
旧库自动补建索引、并发确认/取消/封闭更新混合负载不超额。

---

## 6. 选路与版本语义

- **选路目标**：① 最早到达（带停靠时为完成全部停靠并抵达终点的时刻）；
  ② 到达时刻相同时，按**全程路段编号序列字典序**选路（字符串逐元素比较，
  例如 `("E1","E3") < ("E2","E4")`），不逐段独立打破平局。
- **算法**：
  1. 时间依赖的 Dijkstra（FIFO 网络）计算最早到达时刻 `T`（带停靠时逐程
     复合：各程最早到达映射与“停留结束时刻”均为单调非降，逐程取最早即
     全局最早，同时校验各站窗口可行）；
  2. 在“最终到达不晚于 `T`”的状态空间做多标签搜索，取 `(到达时刻, 编号序列)`
     最优者。无停靠时状态为节点；带停靠时状态为 `(节点, 已完成停靠数)`，
     抵达当前轮到的站点即完成停靠（等窗 + 停留），途经未来站点不产生停靠。
     第二阶段用于处理“节点等候/停靠窗口把不同路径的到达时刻拉平”后，
     单标号算法可能选错字典序路径的问题。
- **多人汇合点**（`POST /api/meeting`）：对每个候选节点，各访客复用上述
  单人 `plan` 独立求“最早到达 + 全程编号序列字典序最小”路线；丢弃任一访客
  不可达或晚于最晚汇合时刻的候选；对合格候选取
  `(最晚个人抵达, 抵达时刻总和, 候选节点编号)` 字典序最小者。每段路线的
  个人平局仍按全程路段编号序列处理，汇合点选择本身不改变单人路线语义。
  反向时间窗对各访客独立生效，逐段结果同样显示其实际通行方向。
- **反向时间窗**：在节点扩展时，除原方向出边外，配置了反向窗的入边在
  窗内提供“反向出边”。`(就绪时刻) -> 最早可行进入时刻` 对每个方向都是
  单调非降函数（FIFO：等待窗开放/封闭结束只会把进入时刻向后推），故
  时间依赖 Dijkstra 与多标签选路无需改变；方向按进入时刻取定，重放获胜
  序列时按当时所在节点确定方向。反向窗无效或重叠时图导入在事务前校验
  失败，整次导入拒绝、旧版本保留。
- **容量确认**（`POST /api/trips`）：确认在单个 `BEGIN IMMEDIATE` 写事务
  内完成“固定当前版本与世代 → 用容量约束重新规划（**按需读取**占用）→
  原子写入行程”。规划不预加载该世代的全部占用：引擎把占用源当作
  `edge_id -> [(enter, leave, people)]` 的惰性视图，只有寻路实际触及的
  **有容量**路段才触发一次按路段查询（`db.edge_occupancy` 经
  `idx_trip_seg_edge(edge_id, leave_ts)` 索引定位，并过滤
  `leave_ts <= 出发时刻` 的占用——半开区间端点相接本就不可能阻挡），
  结果按路段缓存，每条路段在一次确认内至多读取一次；未触及路段上的
  行程增长不会使单次确认的读取量线性增长（基准见 5.1 节）。所有占用
  读取与行程写入同属该串行写事务，占用视图固定（版本与世代在事务
  开始时确定），并发确认、取消、改签、图发布、版本恢复与封闭更新不会
  混入，也不会混用世代。容量约束对每个 `(路段, 人数)` 预先把半开占用
  用扫描线合并成“现有占用 + 本团人数 > 容量”的受阻区间并集；
  `earliest_enter` 把它当作与封闭区间同口径的第三类半开约束（整段
  通行区间不得与之重叠，可在节点等待其结束），方向窗跨越等待边界后
  重新判定。该函数关于就绪时刻仍单调非降（FIFO），故两阶段选路的
  正确性不变；人数本身超过容量的路段对该团体恒不可用。SQLite 写事务
  全局串行，并发确认看到的占用互不重叠，绝不超额；规划失败或版本
  冲突时事务回滚，不留下任何占用。普通 `/api/route` 与 `/api/meeting`
  不提供占用，引擎容量约束完全不参与，返回结构与语义保持不变。
- **行程改签**（`POST /api/trips/{id}/reschedule`）：改签与确认、取消、
  图发布、版本恢复、封闭更新共用同一个 `BEGIN IMMEDIATE` 串行写事务，
  容量绝不超额。事务内固定当前版本与世代并依次校验：行程存在、仍为
  `confirmed`、计划出发时刻仍晚于当前时刻（半开边界）、请求修订号与
  库内 `revision_no` 一致、可选 `data_version` 与当前版本一致；随后以
  **原行程起点**为起点重新规划，按需占用视图在 SQL 层
  （`ts.trip_id <> 本行程`）**排除本行程的旧逐段占用**——新路线既不
  被自身旧路线阻挡也不重复计量，其余同世代未取消行程照常计入；规划
  成功后在同一事务内**一次性替换**行程行的终点/出发与到达时刻/避台阶/
  人数/`version_id`/`generation_id` 并整表重写逐段占用，`revision_no`
  以条件更新（`WHERE trip_id=? AND status='confirmed' AND revision_no=?`）
  递增，为并发改签/取消提供兜底。任一校验或规划失败即回滚，原行程与
  原占用逐字节不变。改签成功的行程随当前版本/世代计量（封闭更新后
  仍是同世代；图重新发布/恢复后则迁入新世代，旧世代不再统计它）。
  旧库 `trips` 表无修订号列时首次启动自动 `ALTER TABLE` 补列，既有
  行程初始修订号回填为 `1`。
- **图发布世代与容量计量**：每次步道图发布与版本恢复开启新世代
  （世代号即新版本号），封闭记录更新沿用当前世代。行程行同时记录
  `version_id`（规划所用数据版本）与 `generation_id`（容量计量口径）；
  确认只读取**同一世代**未取消行程的占用。重新发布/恢复后新世代从
  空占用开始独立计量，旧行程行原样保留（可查询、可取消，取消只释放
  其所属世代）。旧库首次启动自动迁移：`versions` 重建并按发布语义
  回填世代（图发布/恢复各自开世代，封闭更新沿用上一世代），
  `edges` 以 `ALTER TABLE` 增加可空 `capacity` 列（既有版本容量不限），
  `trip_segments` 以 `CREATE INDEX IF NOT EXISTS` 补建
  `idx_trip_seg_edge(edge_id, leave_ts)` 索引（按需读取按路段定位）。
- **原子发布与版本一致**：
  - 每次导入（图或封闭）在一个 SQLite 写事务中完成全部校验、复制与版本指针切换，
    失败整体回滚；行程确认/取消使用同一 `BEGIN IMMEDIATE` 写事务队列，与导入、
    恢复及彼此之间严格串行提交，保证并发确认不超额、不会读到半个版本；
  - 每次查询在开始时读取一次 `current_version` 并固定，**整次请求只用同一版本**，
    导入进行中或刚完成都不会看到半个版本；
  - 历史版本在库中保留，可按版本号追溯。
- **版本恢复**（`POST /admin/restore`）：把指定历史版本的节点、路段、封闭记录与
  反向通行窗整体复制为**新的**生效版本——版本号继续递增、历史版本原样保留，
  绝不回拨 `current_version` 指针。恢复与导入在同一 `BEGIN IMMEDIATE` 写事务
  队列中串行提交，并发时生效顺序即提交顺序；查询侧仍按请求开始时的版本指针
  读取，只会看到恢复前或恢复后的完整状态。目标版本不存在或参数非法时拒绝
  操作，生效版本不变。

---

## 7. 项目结构

```
app/
  config.py        # 环境变量配置
  timeutil.py      # ISO 8601 <-> 纪元秒（UTC）
  schemas.py       # 导入/查询负载校验（含停靠点 stops、多人汇合 meeting、反向窗、容量、行程确认）
  precheck.py      # 联合预检：候选图+封闭记录只读批量校验（收集全部错误及位置）
  db.py            # SQLite 版本化、图发布世代、原子发布与行程持久化（含按路段占用查询、修订号、行程原子替换）
  routing.py       # 时间依赖最短路 + 字典序选路引擎（含停靠、汇合、反向窗、容量占用）
  booking.py       # 容量确认/改签/取消：串行写事务、按需占用视图、世代计量、版本/修订号冲突
  main.py          # FastAPI 路由
  sample_data.py   # 内置示例数据
  cli.py           # init-db / import-* / restore / serve
data/              # 示例 JSON 与本地数据库
scripts/smoke_test.py
scripts/bench_occupancy.py  # 容量确认按需读取占用的性能基准（见 5.1 节）
tests/             # 标准库 unittest 测试（228 个用例）
Dockerfile
docker-compose.yml
docker-entrypoint.sh
```

### 运行测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖半开区间边界、节点等候、封闭穿越判定、方向、避台阶、字典序平局、
按顺序停靠（窗口等待、顺序约束、全程字典序、起点/终点即站点）、
多人汇合（候选筛选、汇合时刻/抵达总和/节点编号三级择优、到达后等待、
逐人避台阶与出发时刻、负载校验）、
反向时间窗（进入时刻判定方向、途中跨窗边界走完、半开边界、
封闭对两方向的全程约束、等窗/等封闭、多窗相接与等待下一窗、
避台阶、与停靠/汇合/字典序平局的组合、逐段实际方向、
无效/重叠窗拒绝整次导入并保留旧版本、版本快照与复制）、
联合预检（全部错误收集与原数组位置、候选图交叉校验封闭引用、
图无效时封闭引用不误报、通过时计数、预检不改变数据版本）、
版本恢复（整体恢复为新生效版本、版本号递增且历史可追溯、
非法参数与不存在版本拒绝且生效版本不变、与发布并发时串行生效、
旧库 versions 表自动迁移）、
容量确认（容量字段校验与缺省不限、半开占用端点相接、同发与逐段
流水等待、人数超容拒绝、与封闭/反向窗/避台阶组合、FIFO 不提前、
原子确认/查询/取消幂等不重复释放、并发确认不超额、封闭更新同世代
继续计量、重新发布与版本恢复开新世代且旧行程可查可取消、
旧库世代回填与容量列迁移、版本冲突不写入、普通路线查询语义不变）、
按需读取占用（未触及路段的行程增长不使单次确认读量线性增长、
触及路段至多读取一次、按需视图与全量预加载规划结果一致、
不晚于出发时刻即结束的占用读取时过滤、占用查询走索引无整表扫描、
旧库自动补建索引、并发确认/取消/封闭更新混合负载不超额）、
行程改签（成功改签一次性替换逐段/人数/版本/世代且修订号递增、
起点与编号不变、先排除自身旧占用且他人占用照常计量、容量等待与
避台阶/封闭/反向窗组合、封闭更新同世代与重发/恢复后迁入新世代、
旧库行程初始修订号为 1 的自动迁移、行程不存在/已取消/已出发/
修订号不符/data_version 不符/新时刻不晚于当前/参数非法/新路线
不可行等拒绝均保持原行程与占用不变、并发改签与取消串行按修订号
冲突裁决、查询与取消字段行为兼容）、
无路线、原子发布回滚与版本快照等场景。
