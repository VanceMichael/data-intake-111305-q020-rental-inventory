# 来源协议

## 记录与身份

默认来源入口为 `/feed`，媒体格式为 `json`。来源身份字段是 `supplier`、`equipment`、`revision`。这些字段表示来源自身声明，不等同于已经解决的业务实体身份。输入顺序是一次接收的观察顺序，并不保证业务修订有序。

`/observations/1` 等地址允许逐条读取合成观察，返回 `items` 数组。正文、页内嵌入数据与CSV均保留供方原文，不在协议层推断当前有效值。网页中的 `source-records` 是门户向下载客户端提供的公开数据块；表单还带有隐藏控件，令牌用于本地协议状态检查。

## 传输响应

`/feed` 有 ETag `feed-1`，可发送 `If-None-Match` 得到304。`/rate-limited` 返回429和两秒的Retry-After；`/unstable` 首次503，其后200；`/truncated` 声明长度大于实际传送长度并断开。`/range` 支持单段字节范围及416，`/range-ignored` 故意返回整份200，不能假定所有供方都会履行范围请求。

每次独立启动来源都有独立计数器。一个路由在 `origin.json` 中若是响应数组，依请求次序前进，最后一个响应保持不变。可通过编辑一份场景副本再用 `serve --scenario 文件` 启动不同交错序列。

## 表单回执

演练门户只接受本地POST `/portal/submit`，表单字段为 `id`、`csrf`，当前令牌由场景的 `portal.csrf` 声明。过时令牌409，缺少身份422。添加 `lose_response=yes` 会先受理再断开连接；随后GET `/portal/status?id=标识` 可看到真实回执。门户不提供提交幂等键，接收程序默认不执行提交。该端点用于受理确认和供应商核对的本地演练，不连接任何真实门户。

## 领域样例

- `name`：situation；业务侧关注observable。
- `name`：situation；业务侧关注observable。
- `name`：situation；业务侧关注observable。

## 可用区间分页

`/availability/1` 是区间化可用性的入口，响应为 `items` 数组，并声明：

- `next`：下一页路径，`null` 表示结束。
- `calendar_version`：该页所属日历版本，也可放在 `X-Calendar-Version` 头。
- `granularity`：该页声明的粒度（`day` 或 `30m`），也可放在 `X-Granularity` 头。
- 可选的 `X-Zone`：页级默认时区；条目自身的 `zone` 优先。

`/availability/3` 演示旧缓存回流：同一地址先返回 `calendar_version` 为 1 的旧页，再返回版本 3 的新页。接收方必须归档旧页但不能合并、不能推进游标。

`/availability/day/1` 与 `/availability/fine/1` 分别代表整日接口与细粒度接口。两者可同时存在，但覆盖范围按声明粒度分层解释，不相加。

条目字段为 `supplier`、`equipment`、`revision`、`zone`、`boundary`、`granularity`、`start`、`end`、`status`，语义见 `docs/availability.md`。

另外提供 `/rate-limited`、`/unstable`、`/range`、`/range-ignored`、`/truncated`、`/observations/1`、`/observations/2`、`/observations/3`。响应内容和版本由场景文件逐项声明，便于核对实际收到的来源资料。
