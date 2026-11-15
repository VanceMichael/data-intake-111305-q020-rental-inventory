# 可用区间、日历发布与预约复核

本文说明在原始采集之上新增的区间化可用性能力：如何保留每家供方的时区、边界约定与修订，如何把相邻时段拼成可读窗口，如何在不抹掉来源冲突的情况下发布日历，以及如何只标出受影响的已有预约而不移动行程。

原始响应仍然只追加保存在归档库中。本文描述的都是**解释层**，可以随业务规则独立演进；任何时候都能回到某次抓取核对原始内容。

## 术语

- **声明（declaration）**：供方对某台设备某一段时间的可用性陈述。字段为 `supplier`、`equipment`、`revision`、`zone`、`boundary`、`granularity`、`start`、`end`、`status`。
- **区间（segment）**：解释层把全部有效声明切成互不重叠的最小片段。每个片段带状态、原因和完整来源列表。
- **窗口（window）**：把相邻且同状态的区间拼起来，用于阅读。窗口保留其内部每个区间与声明，不丢失可追溯性。
- **解释版本（version）**：一次发布的不可变快照。窗口、来源、撤消记录与预约复核清单在同一个事务内写入。
- **日历版本（calendar_version）**：来源声明的日历版本，用于识别旧缓存回流。它和解释版本不是一回事。

## 时区与边界

- `zone` 是**声明自己的**时区，逐条保留，不按供方合并，也不按接收顺序推断。
- `granularity: day` 时 `start`/`end` 是本地日期。状态区间存储为 UTC 半开区间，但日边界按供方本地午夜解析，所以夏令时切换日会是 23 或 25 小时。
- `granularity` 为半小时段时 `start`/`end` 是带偏移的时间戳，或按 `zone` 解释的本地时间。
  - 本地时间在回拨日出现两次时**必须**给出偏移或 `start_fold`/`end_fold`，否则报 `AmbiguousLocalTime`，不猜。
  - 本地时间在拨快日不存在时报 `NonexistentLocalTime`。
  - 整日边界落在被跳过的午夜时，取当地第一个存在的时刻。
- `boundary` 支持 `half_open`（`[start, end)`，默认）与 `closed`（`[start, end]`）。闭区间在整日粒度上把结束日期算入，在半小时粒度上把结束时刻算入，存储时统一转成半开区间。边界写法在来源里原样保留，解释结果里同时给出 `boundary` 与 `start/end_utc`。

## 修订、重叠与撤消

- 同一 `(supplier, equipment, source, zone, boundary, granularity, 解释后的区间)` 上，较高 `revision` 取代较低 `revision`。
- 覆盖范围不同但时间重叠的声明**不会**互相取代，也不会按输入顺序取最后一条。
  - 同粒度互相矛盾：该段状态为 `conflict`，`deciding_declaration_ids` 同时列出双方，原因 `overlapping_declarations_conflict`。
  - 不同粒度：细粒度声明决定自己范围内的状态，整日声明保留为 `refined` 来源，原因 `finer_granularity_refined_coarse_declaration`。两种覆盖范围**分层解释，绝不相加**。
- `cancelled` 表示撤消，不是“从未开放”：
  - 该段仍存在，状态为 `cancelled`，原因 `cancelled_interval_retained`。
  - 撤消声明是 `decides` 来源，被撤消的早期可用声明以 `retracted_prior` 保留在来源列表里。
  - 同时生成撤消记录，含 `supersedes`、`previously_available`、`cause`。

## 分页抓取与恢复

入口为 `GET /availability/1`，响应可带 `next`（下一页路径）、`calendar_version`、`granularity`，也可用 `X-Calendar-Version`、`X-Granularity`、`X-Zone` 头部声明。分页实现见 `rental_inventory/ingest.py`。

- **每次尝试都先归档**原始响应（含失败响应与旧缓存），再决定是否接受。
- **接受条件**：HTTP 200 且 `calendar_version` 不低于本次运行已确认的版本。
- **旧缓存回流**：版本更低的响应会被归档但不合并，游标不前进；重试后拿到不低于已确认版本才继续。重试耗尽时抛 `StaleCacheExhausted`，运行保持可恢复，不会用旧缓存覆盖已确认日历。
- **可恢复**：每次尝试后把已接受页、游标、已确认版本、粒度集合写入目录库。用同一 `source` 与入口路径再次打开即从第一个未接受页继续，已接受页不会重复抓取。
- **传输失败**：429/503 等按 `Retry-After`（上限 5 秒）退避重试；耗尽后抛 `FetchFailed` 并保留游标。
- **粒度切换**：整日接口与细粒度接口可以同时存在（`/availability/day/1` 与 `/availability/fine/1`）。每条声明保留其所在页声明的粒度，两种接口的覆盖范围只分层、不相加；运行摘要会列出本次涉及的全部粒度。

## 发布、查询与一致性

`rental_inventory/catalog.py` 管理发布版本。

- `publish(declarations, calendar_version=...)` 在**一个写事务**内完成：版本号分配、日历版本栅栏、声明、撤消记录、窗口、预约复核清单。
  - 栅栏：若本次 `calendar_version` 低于已发布的日历版本则拒绝（`StalePublication`），晚到响应不能覆盖已确认的新日历；需要人工覆盖时必须显式 `allow_stale=True`。
  - 每次发布都是新版本，旧版本不会被改写。
- `query(version=None)` 在**一个读事务**内读取版本头、声明、窗口与复核清单，并用快照摘要校验一致性。查询看到的窗口和复核清单永远属于同一次解释；并发发布也不会交叉。
- 摘要覆盖的内容与顺序固定（声明按编号、窗口按序号、复核按预约编号），任何一行的改动都会在读取时被发现。

## 预约影响

已有确认预约从不被采集器移动，也不被改写。`rental_inventory/impact.py` 与 `Catalog.query` 只输出复核清单：

- 预约区间落在 `cancelled`、`unavailable` 或 `conflict` 上，或没有被完整可用的区间覆盖时，`requires_review` 为真，`action` 恒为 `review_only_do_not_move_booking`。
- `reason` 取值：`supplier_availability_changed`、`supplier_declaration_conflict`、`booking_outside_declared_coverage`。
- `differences` 只给出与预约重叠的那一段，并列出触发复核的来源差异：声明编号、角色、来源、抓取编号与接收时间、修订号、粒度、时区、边界、声明状态、原始起止文本与 UTC 起止。
- 预约与声明都先换算成 UTC 再比较，跨时区与跨夏令时不改变判断。
