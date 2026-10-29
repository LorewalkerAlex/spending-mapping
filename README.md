# spending-mapping

把信用卡账单邮件变成 Cashew 交易的服务：抓取 → 解析 → 映射 → 推送 → 审核。

当前状态：**本地骨架与字段契约已完成并通过测试，尚未连接服务器**。

## 这个仓库负责什么

| 做 | 不做 |
| --- | --- |
| 从 IMAP 抓取账单邮件，按 sha256 落盘 | 不做聚合报表（归消费方按需计算） |
| 解析账单为逐笔交易 | 不维护独立业务数据库（可由 eml + 规则 + Cashew 重建） |
| 归一商户名、映射到 Cashew 分类 | 不直接读写 Cashew 的 SQLite（只用 sync 接口） |
| 生成并推送 Cashew 交易 payload | 不覆盖用户在 Cashew 手记的交易 |

设计口径见 [`docs/PIPELINE-DESIGN.md`](docs/PIPELINE-DESIGN.md)，Cashew 接口细节见 [`docs/CASHEW-SYNC-API.md`](docs/CASHEW-SYNC-API.md)。

## 目录

```
src/spending_mapping/
  records.py         导入集数据模型（净额后的逐笔交易）
  categories.py      Cashew 分类/钱包口径与兜底规则
  cashew_payload.py  31 字段 payload 构建、change 包装、内容指纹
  timestamps.py      推送时间戳序列（严格递增、复现服务端夹紧）
tests/               纯本地测试，无需网络与服务器
config/
  categories.json    14 个分类与唯一钱包（来自客户端备份，2026-10-29）
  category-map.json  旧 18 类 → Cashew pk 的对照，含兜底分类
```

## 本地测试

```bash
python run-tests.py
```

只用标准库，不需要安装依赖。40 个用例覆盖：payload 字段契约（数量/类型/长度）、分类解析与兜底、时间戳单调性、导入集的守恒与覆盖。

数据相关用例需要 `net-transactions.json`（真实交易，不入库）。默认找仓库内 `data/`，或用环境变量指向别处：

```bash
SPENDING_DATA_DIR=/path/to/import-set python run-tests.py
```

缺失时这些用例会跳过而不是失败。

## 关键事实（不要重新推断）

- `amount` 是 double，**负数=支出**；符号即方向，`income` 只影响展示
- `categoryFk` 是字符串外键：默认分类是 `"1"`–`"11"`，用户新建的是 UUID
- 普通一次性支出的写入值与客户端一致：`paid=true`、`skipPaid=true`、`reoccurrence=3`（monthly）、`periodLength=1`、`type=null`
- `name ≤ 250`、`note ≤ 500`（客户端 `NAME_LIMIT` / `NOTE_LIMIT`）
- Cashew **没有商户实体**，按 `name` 分组就是统计口径；商户名归一等于定义统计单元
- 服务端按 `modifiedAt` 做 last-write-wins，旧时间戳被丢弃，超前服务器时间 2 分钟会被夹紧

## 未映射交易

导入集里有 117 笔（90 个商户，合计 -5983.59）在旧映射表中没有分类。它们落到兜底分类「其他」并计入待审队列——这是审核界面的第一批工作，不是数据错误。`tests/test_import_set.py` 把这个基线固化成了断言，变化时会失败提醒。

## 部署（后续步骤）

尚未开始。计划：推送 Git → 服务器拉取到持久路径 → `.env` 配置凭据（`examples/.env.example` 为模板，真实 `.env` 不入库）→ systemd timer 定时拉取。服务器侧的临时产物遵循项目约定，统一放在 `/tmp/home-server-ops/<任务标识>-<唯一后缀>/`。
