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
python -m spending_mapping sync                # dry-run：只统计会推多少、多少未抵消
python -m spending_mapping sync --limit 10     # 试点 10 笔
python -m spending_mapping sync --apply        # 全量导入
python -m spending_mapping reset               # 预览将删除多少（src_ 前缀）
python -m spending_mapping reset --yes         # 真正删除
```

- `sync` 默认 dry-run，必须 `--apply` 才写
- `reset` 只删主键以 `src_` 开头的交易（我们导入的），**用户手记的 UUID 交易不动**
- 删完可以直接再 `sync --apply` 重新导入

## 未映射交易

从零起步（不继承旧 merchants/categories 映射）：所有交易的分类初始都落兜底「其他」，名字用解析器的 `display_name`。人工审核逐步把「描述 → 分类」写进 `review-log.jsonl`，越审越少。审核粒度支持批量（按 description）与单笔。

## 部署（Docker Compose）

命名约定：**服务名统一为 `spending`**（与 cashew/tududi/beszel 一致），仓库名 `spending-mapping`、Python 包 `spending_mapping` 只是项目标识，不参与服务器路径。

- 代码 `/srv/apps/spending/`，数据 `/srv/persistent/data/spending/`，密钥 `/srv/persistent/secrets/spending/spending.env`
- `Dockerfile`：`python:3.12-slim`，无第三方依赖，入口 `python -m spending_mapping`
- `compose.yaml`：`name: spending`、服务与镜像名 `spending`，`env_file` 指向密钥目录，`/srv/persistent/data/spending` 绑定到容器 `/data`，`restart: unless-stopped`
- 常驻进程 + 内部轮询（`MAIL_POLL_INTERVAL_SECONDS`），Docker 里没有 systemd timer

目前入口只做「邮件拉取」；Cashew 同步（push/pull）是下一步，会加进同一个服务循环。密钥模板见 `examples/spending.env.example`，真实 `.env` 不入库。
