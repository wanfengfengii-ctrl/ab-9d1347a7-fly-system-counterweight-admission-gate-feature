# 剧场吊杆配重装载安全链路

剧场装台场景：多名技师可能在吊杆两端同时登记配重片。本系统保证**逐片装载的原子裁决**——
任何并发下合计重量都不会突破吊杆核定值，配重片标识全库只能成功登记一次，被拒绝的请求绝不落库。
装台调整时还可以把**已登记的配重片整体转移**到另一根吊杆：只改归属、不清空、不重新登记，
原始重量与登记时间保留。装台复核发现实物标称重量录错时，技师可在明细中直接**修正重量**：
不删除配重片、不重新登记，只改现有记录的重量，配重标识与最初登记时间保留。

## 架构

- **web**（React + Vite）：选择预置吊杆，录入配重片唯一标识与整数克重量，展示当前总重、
  剩余量、已接纳明细或明确拒绝原因。明细中每片配重片提供“转移”与“修正重量”：前者选择目标
  吊杆确认后即调用转移接口；后者输入新的整数克数确认后提交修正，成功则展示旧值→新值与刷新
  后的总重 / 余量。页面所有状态均来自接口，刷新后与数据库一致。
- **api**（FastAPI + SQLAlchemy）：在 PostgreSQL 中以 `SELECT ... FOR UPDATE` 行级锁
  串行化同一吊杆上的并发装载；转移时按吊杆编号**固定顺序**锁定源、目标两根吊杆，在同一事务
  内确认配重片仍属于源杆并校验目标余量，随后只更新现有装载记录的归属。修正重量时在同一事务
  内先锁定路径指定的吊杆，再确认装载记录仍归属于该杆，以新旧重量之差重新核算容量（恰好达到
  核定值允许），随后只更新现有记录的重量。
- **db**（PostgreSQL 16）：仅保存吊杆（G-01 / G-02）与成功装载记录两张表。

Compose 只定义 **web** 与 **api** 两个应用服务（db 为数据基础设施）。

## 运行

```bash
docker compose up --build
```

- 页面：http://localhost:5173 （可用 `WEB_PORT` 覆盖宿主端口）
- 接口：http://localhost:8000/docs （可用 `API_PORT` 覆盖宿主端口）

```bash
WEB_PORT=9000 API_PORT=9001 docker compose up --build
```

## 验收规则

- 固定夹具：`G-01`（核定 30000 克）、`G-02`（核定 50000 克），服务启动时自动建表播种。
- 单片重量仅接受 100～25000 克，且必须是 JSON 整数——字符串 `"100"`、小数 `100.0`
  等一律 422 拒绝，不做隐式转换。
- 接纳条件：当前总重 + 本片重量 ≤ 核定值（恰好相等允许）。
- 配重片标识全库唯一，只能成功一次；失败请求不写入数据库。
- 转移：把一片已登记配重片从源吊杆移到目标吊杆，**只更新现有装载记录的归属**，
  原始重量与登记时间保留；目标杆当前总重 + 本片重量 ≤ 核定值才允许。
  固定顺序锁定两根吊杆，相反方向的并发转移不会死锁；配重片已被其他终端移走时
  返回 `POSITION_CHANGED`，数据库保持原归属。
- 修正重量：复核发现标称重量录错时，把现有记录的重量改为新的整数克数（沿用单片
  100～25000 克范围），**只改重量**，配重标识与最初登记时间保留。同一事务内先锁定
  路径指定的吊杆，再确认装载记录仍归属于该杆，以 `当前总重 − 旧重量 + 新重量` 重新
  核算容量（恰好达到核定值允许）；新重量越界返回 `INVALID_WEIGHT`，导致超载返回
  `OVER_CAPACITY`，记录已被其他终端转移走时返回 `POSITION_CHANGED`——任一拒绝时
  数据库中的原重量保持不变。
- `POST /api/reset`：清空装载记录、恢复两根空吊杆，供每个验收场景开始前调用。

## 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/battens` | 两根吊杆的总重 / 剩余量 |
| GET | `/api/battens/{id}` | 单杆明细（含已接纳配重片列表） |
| POST | `/api/battens/{id}/loads` | 逐片装载裁决，体：`{"piece_id": "...", "weight_grams": 20000}` |
| POST | `/api/battens/{id}/loads/{load_id}/transfer` | 转移已登记配重片，体：`{"target_batten_id": "G-02"}`；响应含 `source` / `target` 两杆刷新后的总重与余量 |
| POST | `/api/battens/{id}/loads/{load_id}/correct-weight` | 修正已登记配重片的标称重量，体：`{"weight_grams": 12000}`；响应含刷新后的总重 / 余量及旧值 `previous_weight_grams` |
| POST | `/api/reset` | 恢复两根空吊杆 |

拒绝响应均带 `accepted: false` 与 `reason`：
`INVALID_WEIGHT`（422）、`INVALID_INPUT`（422）、`SAME_BATTEN`（422）、
`PIECE_ID_EXISTS`（409）、`OVER_CAPACITY`（409）、`POSITION_CHANGED`（409）、
`BATTEN_NOT_FOUND`（404）、`LOAD_NOT_FOUND`（404）。

## 测试（均为真实接口 / 真实数据库，无假接口）

```bash
# pytest：真实并发事务（两个 20000 克并发投向空 G-01，必须仅一笔成功，
# 总重固定 20000 克、剩余量固定 10000 克）
docker compose exec api pytest -v

# Vitest：页面交互 + 真实接口反馈
docker compose exec web npm run test
```

宿主机直接运行（服务已启动时）：

```bash
cd api && API_BASE_URL=http://localhost:${API_PORT:-8000} python -m pytest -v
cd web && API_URL=http://localhost:${API_PORT:-8000} npm run test
```
