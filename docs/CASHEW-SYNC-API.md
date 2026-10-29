# Cashew 同步 API 接入说明

面向后续 Mapping 系统读写 Cashew 数据。所有结论均来自 2026-10-27 ~ 10-28 对 `ghcr.io/vfyuj/cashew-selfhosted:1.4.3` 的真实调用，标注「实测」的条目有对应脚本与输出；标注「未验证」的条目尚未确认，接入前需自行确认。

- 客户端封装：`cashew_api.py`（哈希 `90d7d462…`，直接 import 即可）
- 验证脚本：`06-api-probe.py`（只读探测）、`07-refund-semantics-test.py`（金额语义）、`08-id-roundtrip-test.py`（主键往返，19/19 通过）
- 最后更新：2026-10-28

---

## 1. 部署形态与访问地址

单容器 Dart/Shelf 服务，SQLite 落盘在 `/srv/persistent/data/cashew/db.sqlite`，容器仅绑回环 `127.0.0.1:13004`，对外由 Caddy 反代。

| 用途 | 基地址 | 说明 |
| --- | --- | --- |
| 同机其它服务（推荐） | `http://127.0.0.1:13004` | 跳过 TLS 与反代，最少故障点 |
| 内网客户端 | `https://cashew.eurexis.com:4129` | LAN 站点，受 PAC/Squid 白名单约束 |
| 公网 | `https://cashew.eurexis.com` | 443，同域名不同端口 |

Mapping 系统若与本服务同机部署，**用回环地址**；若分离部署，用内网域名并确认出口代理放行该主机名。

---

## 2. 认证

### 2.1 登录

```
POST /auth/login
Content-Type: application/json

{ "email": "<管理员邮箱>", "password": "<密码>" }
```

成功响应（实测）：

```json
{
  "sessionToken": "…",
  "expiresAt": "2026-11-26T…Z",
  "user": { "email": "…", "isAdmin": true }
}
```

- `sessionToken` 有效期 **30 天**，到期需重新登录；无 refresh 机制（未验证是否存在续期端点）。
- 失败为 `HTTP 401`，响应体含错误描述（`cashew_api.py` 会截取前 300 字符抛出）。
- 登录不区分设备，`deviceId` 只在 push 时使用。

### 2.2 携带方式

所有同步接口带 `Authorization: Bearer <sessionToken>`。

---

## 3. 同步接口

服务端是「变更日志」模型：每张表每条记录对应一条带自增 `seq` 的变更，客户端按 `seq` 游标增量拉取。

### 3.1 拉取

```
GET /sync/pull?since=<cursor>&limit=<n>
Authorization: Bearer …
```

- `since`：上次拿到的 `nextCursor`，首次传 `0`。
- `limit`：实测 `2000` 可用；上限未验证。
- 响应：

```json
{
  "changes": [
    {
      "seq": 11,
      "table": "Transaction",
      "pk": "src_52b5709348e979cd0f54f549",
      "deleted": false,
      "modifiedAt": 1793152282135,
      "payload": { "…": "…" }
    }
  ],
  "nextCursor": 12,
  "hasMore": false
}
```

`cashew_api.py` 的 `pull_all()` 会自动翻页直到 `hasMore=false`，返回 `(changes, cursor)`。

### 3.2 推送

```
POST /sync/push
Authorization: Bearer …
Content-Type: application/json

{ "deviceId": "home-server-cli", "changes": [ <change>, … ] }
```

响应（实测）：

```json
{ "serverTime": 1793152282135, "conflictCount": 0 }
```

- `deviceId` 是客户端标识，随请求上报；同一设备重复推同一 `pk` 表现为 upsert。
- `conflictCount` 为本次被判为冲突（时间戳过旧）的条目数，**失败条目不回滚同批其它条目**。
- 单批条数上限未验证；导入时按 100~200 条一批较稳妥。

### 3.3 change 结构

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `table` | string | 表名，实测有 `Transaction` / `Category` / `Wallet` |
| `pk` | string | 主键，写入方决定（见 §5.1） |
| `deleted` | bool | 软删除标记，`true` 即删除 |
| `modifiedAt` | int | 变更时间戳（毫秒），参与冲突判定 |
| `payload` | object | 整行快照，形状取决于表 |

删除一条记录 = 推一条 `deleted: true` 的 change（payload 可给原值或最小行）。实测删除后该记录不再出现在「未删除交易」集合中，`seq` 仍保留历史变更。

---

## 4. Transaction payload（31 字段）

整行快照，字段与客户端 Drift 表的 `toJson()` 一一对应。缺失字段不会报错（服务端不校验，见 §5.4），但客户端渲染会异常，**导入时应完整给出 31 个字段**。

### 4.1 主键与关联

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `transactionPk` | string | 必须等于 change 的 `pk` |
| `pairedTransactionFk` | string \| null | 配对交易（转账对侧） |
| `categoryFk` | string | 分类主键，**数字字符串**，如 `"1"` |
| `subCategoryFk` | string \| null | 子分类 |
| `walletFk` | string | 钱包主键。测试用 `"0"` 可写入但不指向真实钱包；导入须用真实钱包 pk |
| `type` | string \| null | 交易类型，普通消费为 null |
| `objectiveFk` / `objectiveLoanFk` / `budgetFksExclude` | string \| null | 预算/目标关联，非目标记账留 null |

### 4.2 金额与展示

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `amount` | int | 金额，**负数为支出、正数为收入**（见 §5.2） |
| `name` | string | 显示名称（商户名） |
| `note` | string | 备注 |
| `income` | bool | 仅影响客户端展示归类，不改变聚合方向 |

### 4.3 时间

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `dateCreated` | int | 交易发生日期（毫秒），列表按此排序与分组 |
| `dateTimeModified` | int | 最后修改时间（毫秒），**参与冲突判定** |
| `originalDateDue` | int | 应付日期；一次性消费与 `dateCreated` 相同 |
| `endDate` | int \| null | 重复结束日期 |

### 4.4 重复与状态

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `periodLength` | int | 重复周期长度，非重复为 `1` |
| `reoccurrence` | int | 重复枚举，非重复为 `3`（实测导入显示正常，其余枚举值未验证） |
| `paid` | bool | 是否已支付，历史消费为 `true` |
| `skipPaid` | bool | 是否跳过支付确认，历史消费为 `true` |
| `createdAnotherFutureTransaction` | bool | 是否已衍生未来交易，`false` |
| `upcomingTransactionNotification` | bool | 到期提醒，`true` |

### 4.5 共享与来源

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `methodAdded` | string \| null | 录入方式，可 null |
| `transactionOwnerEmail` / `transactionOriginalOwnerEmail` | string \| null | 共享账单归属 |
| `sharedKey` / `sharedOldKey` / `sharedStatus` / `sharedDateUpdated` / `sharedReferenceBudgetPk` | string \| null | 共享账单字段，非共享全 null |

---

## 5. 实测语义与硬约束

### 5.1 主键可自定义，且端到端原样保留（实测，19/19）

`08-id-roundtrip-test.py` 验证了两种格式都可写入并原样拉回：

- UUID 形式：`11111111-1111-4111-8111-111111111111`
- 旧记录格式：`src_` + 24 位十六进制，如 `src_52b5709348e979cd0f54f549`

`change.pk`、`payload.transactionPk`、`amount`、`name`、`deleted` 五个维度均未被服务端改写。

**结论：导入历史数据时直接使用旧 `source_record_id` 作为主键，Mapping 追溯链不需要映射表。**

### 5.2 金额符号即方向（实测）

推送 `amount=-101` + `income=false` 与 `amount=+40` + `income=true` 后，客户端首页显示「支出 101 / 收入 40」。

- 聚合只看符号：**负数恒计入支出，正数恒计入收入**。
- `income` 只影响展示归类，不能靠它把负数算成收入。

### 5.3 时间戳必须递增，否则被判冲突（实测）

- 同主键用更新的 `modifiedAt` 推送 → upsert，payload 被覆盖。
- 同主键用更旧的 `modifiedAt` 推送 → `conflictCount=1`，**payload 不被覆盖**。

**约束**：导入脚本每次运行都要用「当前时间 + 每笔递增毫秒」生成 `dateTimeModified` 和 `modifiedAt`，不能沿用上次的旧快照，否则整批被判冲突丢弃。

### 5.4 服务端不校验 payload

任意字段缺失、类型不符都不会被拒绝，脏数据会原样入库并由客户端渲染时才暴露。**校验责任完全在写入方**，导入前必须自检字段完整性与外键有效性。

### 5.5 幂等性

同主键重复推送是 upsert，不产生第二条记录。导入脚本天然可重跑，但重跑会覆盖 `dateTimeModified`（见 §5.3）。

---

## 6. 客户端封装 cashew_api.py

### 6.1 配置

脚本同目录放 `.env`：

```
CASHEW_EMAIL=<邮箱>
CASHEW_PASSWORD=<密码>
```

值不加引号（解析器只在两端 strip 引号，值为空或含首尾空格才需要引号）。凭据读取优先级：环境变量 `CASHEW_EMAIL`/`CASHEW_PASSWORD` → 同目录 `.env` → `CASHEW_CREDENTIALS_FILE` → 交互输入。

登录态缓存在同目录 `.cashew-session.json`（0600，30 天有效）。可用 `CASHEW_SESSION_FILE`、`CASHEW_ENV_FILE` 覆盖两个路径——**将来做成常驻服务时，把两者都指向 `/srv/apps/cashew/` 下的固定位置**，不要留在 `/tmp`。

### 6.2 用法

```python
import sys; sys.path.insert(0, "/srv/apps/cashew")
from cashew_api import Client

c = Client()                       # 默认 http://127.0.0.1:13004
c.ensure()                         # 有缓存用缓存，失效则按 .env 自动登录
changes, cursor = c.pull_all()     # 全量拉取，自动翻页

c.push([{
    "table": "Transaction",
    "pk": "src_52b5709348e979cd0f54f549",
    "deleted": False,
    "modifiedAt": 1793152282135,
    "payload": { /* 31 字段 */ },
}])
```

命令行：`python3 cashew_api.py status|login|logout`。

> 注意：`07-refund-semantics-test.py` 留下过 3 笔测试交易（支出 101 / 收入 40），已在后续清理中删除；当前基线为 0 笔交易。真实导入前用 `status` 或 `pull_all()` 复核基线。

---

## 7. Mapping 系统对接建议

### 7.1 字段映射

导入集 `net-transactions.json`（1364 笔，结构见下）到 Cashew：

| 导入集字段 | Cashew 字段 | 处理 |
| --- | --- | --- |
| `source_record_id` | `transactionPk` + change `pk` | 原样使用 |
| `date`（`YYYY-MM-DD`） | `dateCreated`、`originalDateDue` | 转毫秒时间戳（本地时区 00:00） |
| `merchant` | `name` | 直接用，必要时截断 |
| `category` | `categoryFk` | 需按名称解析为分类 pk（见 §7.3） |
| `amount`（字符串，带符号） | `amount`（int） | 直接转 int，符号已正确 |
| `note` / `description` | `note` | 保留原始描述便于回溯 |
| `gross` / `offset` | — | 不写入 Cashew，留在导入集作为对账凭据 |

未写入 Cashew 的字段意味着账面上看不到退款抵消过程；如需保留，把 `gross`/`offset` 拼进 `note`，例如 `原额 297.00，已抵消 0`。

### 7.2 ID 与幂等

- 主键直接用 `source_record_id`（`src_`+24hex），已验证可往返。
- 重跑同一批 = 覆盖更新，不会重复记账。
- 修改已导入记录的金额或分类时，必须给新的递增时间戳。

### 7.3 待补充的前置数据

导入前还需从服务端拉一次 `Category` 和 `Wallet` 的 `pk`，建立「分类名 → pk」「钱包名 → pk」两张表：

- 导入集里的 `category` 是中文名（如「综合购物」），Cashew 侧需要对应 `categoryFk`；缺失的分类要先建。
- `walletFk` 不能沿用测试值 `"0"`，必须指向真实钱包。

### 7.4 建议的导入流程

1. **dry-run**：只读校验——笔数、金额合计（支出/收入分列）、日期区间、分类覆盖率，与 `net-transactions.json` 对账。
2. **小批量试写**：先推 10 笔，客户端核对显示、分类归属、金额方向。
3. **全量导入**：100~200 笔一批，逐批检查 `conflictCount=0`。
4. **回读校验**：`pull_all()` 后按 `pk` 比对金额、名称、`dateCreated` 与源数据一致率 100%。

---

## 8. 未验证项

接入前需自行确认，不要假定：

- `/sync/pull` 的 `limit` 上限，以及超大响应是否有超时边界。
- 除 `Transaction` / `Category` / `Wallet` 外还有哪些表参与同步（`Budget`、`Objective` 等尚未探测）。
- `Category` / `Wallet` 的 payload 字段形状（仅确认过键与类型，未逐字段记录）。
- `sessionToken` 是否有续期或刷新端点，30 天到期后是否可直接换新。
- `push` 单批条数上限与超时行为。
- 并发推送同一 `pk` 的冲突判定细节（仅验证了串行新旧时间戳）。
- `reoccurrence` 枚举中非重复值 `3` 的语义来源（沿用真实行样例，未验证其它取值）。

---

## 9. 变更记录

| 时间 | 内容 |
| --- | --- |
| 2026-10-28 | 建立文档，收录 §5 全部实测结论与 `08-id-roundtrip-test.py` 的 19/19 结果 |
| 2026-10-27 | 探测接口与 payload 形状（`06`）、确认金额语义（`07`） |
