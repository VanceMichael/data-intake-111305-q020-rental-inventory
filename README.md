# 设备租赁目录的占用时间段采集

这是设备租赁目录的采集与解释程序。原始层请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录；解释层把来源声明转成区间化可用性、发布不可变日历版本，并标出受供方变化影响的已有预约。程序不自动执行门户提交，也不移动已有预约；重复观察分别保留，原始内容按摘要复用。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。时区资料使用运行环境内的系统数据库。运行过程不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。

构建工具及其传递依赖精确锁定在 `requirements-build.txt`。首次准备开发环境时先执行下面命令；之后测试、构建和演示均可离线运行，不在运行命令中安装包。

```sh
python3 -m pip install -r requirements-build.txt
```

```sh
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 -m compileall -q rental_inventory tests
python3 -m rental_inventory demo
```

演示从空的临时目录启动本地来源，真实完成一次 HTTP 获取及归档，并输出原样记录；结束自动清理。本地测试、编译和演示合计一般不超过十秒，可直接展示，不依赖历史日志。

## 常驻来源与接收

```sh
python3 -m rental_inventory serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m rental_inventory capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供协议演练。

## 分页同步、日历发布与预约复核

```sh
python3 -m rental_inventory sync --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --catalog .runtime/catalog.sqlite3 --source local-source
python3 -m rental_inventory book --catalog .runtime/catalog.sqlite3 --booking-id B1 --supplier hire-a --equipment EQ-9 --start 2026-10-25T01:30:00+02:00 --end 2026-10-25T02:00:00+02:00
python3 -m rental_inventory query --catalog .runtime/catalog.sqlite3
```

- `sync` 按 `next` 逐页抓取，每页先归档再决定接受；写入已接受页、游标和已确认日历版本，中断后再次执行同一命令即可从第一个未接受页继续。
- 旧缓存回流（`X-Calendar-Version` 低于已确认版本）会归档但不合并、不推进游标，重试耗尽时以 `incomplete` 结束并保持可恢复。
- `sync` 结束时发布一个不可变解释版本：窗口与预约复核清单在同一个事务内写入、同一个读事务读出，摘要校验保证两者属于同一次解释。
- 已有预约只出现在复核清单里，`action` 为 `review_only_do_not_move_booking`；来源差异逐条列出声明编号、抓取编号、修订号、时区、边界与粒度。
- 各模块分工：`ingest.py` 负责可恢复分页，`availability.py` 负责区间语义与冲突保留，`catalog.py` 负责版本发布与一致查询，`impact.py` 提供复核预览。

详细规则见 `docs/availability.md`。

## 开发资料

来源协议见 `docs/source-protocol.md`，区间、发布与复核规则见 `docs/availability.md`，领域观察关系见 `tests/test_source_contract.py`。`fixtures/business_cases.json` 保存四类业务情况的名称、背景与可观察口径（跨时区边界、旧缓存回流、粒度切换、预约复核），`tests/test_business_cases.py` 会把这些口径当作断言执行。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `rental_inventory` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m rental_inventory demo` 与 `sync`/`query`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
