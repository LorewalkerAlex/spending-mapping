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
  categories.json    14 个分类与唯一钱包（来自客户端备份，2026-10-29），含兜底分类 pk
```

## 本地测试

```bash
python run-tests.py
```

只用标准库，不需要安装依赖。40 个用例覆盖：payload 字段契约（数量/类型/长度）、分类解析与兜底、时间戳单调性、导入集的守恒与覆盖。

数据相关用例需要 `net-transactions.json`（真实交易，不入库）。默认找仓库内 `data/`，或用环境变量指向别处：

```bash
DATA_DIR=/path/to/import-set python run-tests.py
```

缺失时这些用例会跳过而不是失败。

## 关键事实（不要重新推断）

- `amount` 是 double，**负数=支出**；符号即方向，`income` 只影响展示
- `categoryFk` 是字符串外键：默认分类是 `"1"`–`"11"`，用户新建的是 UUID
- 普通一次性支出的写入值与客户端一致：`paid=true`、`skipPaid=true`、`reoccurrence=3`（monthly）、`periodLength=1`、`type=null`
- `name ≤ 250`、`note ≤ 500`（客户端 `NAME_LIMIT` / `NOTE_LIMIT`）
- Cashew **没有商户实体**，按 `name` 分组就是统计口径；商户名归一等于定义统计单元
- 服务端按 `modifiedAt` 做 last-write-wins，旧时间戳被丢弃，超前服务器时间 2 分钟会被夹紧

## 命令行

`python -m spending_mapping` 不带参数时跑常驻拉取服务（容器默认行为）；带子命令时执行对应动作：

```bash
python -m spending_mapping import                 # dry-run：只统计会导多少
python -m spending_mapping import --limit 10 --apply
python -m spending_mapping import --apply         # 全量导入（分类全为「其他」）

python -m spending_mapping review                 # 列待审分类与待审退款
python -m spending_mapping apply-category --name 盒马鲜生 --category 2 --apply
python -m spending_mapping apply-refund --record src_xxx --offset src_yyy --apply
python -m spending_mapping apply-refund --record src_xxx --income --category 11 --apply

python -m spending_mapping reset                  # 预览
python -m spending_mapping reset --yes            # 删除我们导入的交易
```

- 写操作默认 dry-run，必须 `--apply` 才落库
- `reset` 只删主键 `src_` 开头的交易，**用户手记的 UUID 交易不动**
- 导入去重双保险：`imported.jsonl` 台账 + 远端已有 pk

## 数据模型

| 文件（`DATA_DIR` 下） | 作用 |
| --- | --- |
| `imported.jsonl` | 入账台账，append-only（imported/deleted 事件） |
| `review-log.jsonl` | 审核决策，append-only（分类：商户名→pk；退款：offset/income） |
| `evidence/cmb-email/*.eml` | 账单原件 |

**导入与审核是两步**：导入只做「解析 → 净额 → 清洗名字 → 去重 → 推送」，分类一律落兜底「其他」；审核时再从 Cashew 拉回现值，按商户名分组逐条决策。这样导入不依赖分类拍板，审核看到的也是账本真实状态。

## 部署（Docker Compose）

命名约定：**服务名统一为 `spending`**（与 cashew/tududi/beszel 一致），仓库名 `spending-mapping`、Python 包 `spending_mapping` 只是项目标识，不参与服务器路径。

- 代码 `/srv/apps/spending/`，数据 `/srv/persistent/data/spending/`，密钥 `/srv/persistent/secrets/spending/spending.env`
- `Dockerfile`：`python:3.12-slim`，无第三方依赖，入口 `python -m spending_mapping`
- `compose.yaml`：`name: spending`、服务与镜像名 `spending`，`env_file` 指向密钥目录，`/srv/persistent/data/spending` 绑定到容器 `/data`，`restart: unless-stopped`
- 常驻进程 + 内部轮询（`MAIL_POLL_INTERVAL_SECONDS`），Docker 里没有 systemd timer

目前入口只做「邮件拉取」；Cashew 同步（push/pull）是下一步，会加进同一个服务循环。密钥模板见 `examples/spending.env.example`，真实 `.env` 不入库。
