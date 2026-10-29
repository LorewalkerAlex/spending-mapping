# Mapping 数据链路设计

基于 2026-10-27 ~ 10-29 的实测结论与用户决策。所有"实测"条目有脚本与输出可查，"未验证"条目标注在 §11，不能当成事实使用。

## 1. 目标与不变量

**目标**：账单邮件进来后自动完成「解析 → 映射 → 入账」，人工只处理机器没把握的部分；账本在 Cashew 里始终完整，且能随时重建。

**四条不变量**

1. **邮件原始字节不可变**。`sha256` 是它的身份，也是解析缓存的键；任何清洗都不得回写原件
2. **交易身份由邮件决定**，不由 Cashew 决定。`record_id = src_ + sha256(来源类型 + 邮件哈希 + 行定位)[:24]`，只依赖字节与行位置，改规则不改变 ID
3. **mapping 不持有业务数据库**。内存里的交易视图可由 `eml + 规则 + Cashew` 重建；唯一不可推导的是人工审核决策
4. **用户在 Cashew 里手记的交易，永不被覆盖**。它们的 ID 不在我们的集合里，属于外部数据

## 2. 三个数据源与对齐键

| 源 | 内容 | 角色 | 访问方式 |
| --- | --- | --- | --- |
| `eml` 文件 | 账单原件，sha256 命名 | 应然（账该怎么记） | 本地目录，IMAP 拉取 |
| 规则日志 | 人工映射决策，append-only | 归一与分类的依据 | 本地 JSONL |
| Cashew | 用户看到的账本 | 实然（账现在长什么样） | `/sync/pull?since=0` |

**对齐键**：`record_id`

- 账单产生的交易 → `src_…`，两侧应一致
- 用户手记的交易 → 客户端 UUID，只存在于 Cashew 侧 → 标记 `external`，只读
- 只在应然侧 → 待新增（待推）
- 两侧都有 → 逐字段比对，按 §7 裁决

**关键事实**：Cashew 服务端是**变更中继**，`sync_records` 每行一条、只存最新值，没有全表快照接口。`pull?since=0` 拿到的是"被客户端推送过的所有行"。所以：

> 冷启动的完整性取决于客户端是否推过全量。旁证是客户端备份（`GET /backup/<filename>`，SQLite 原件），可用来核对行数与金额。

## 3. 每笔交易的状态

```
mapping_source   auto | human_approved | human_edited        （谁定的 name/category）
origin           bill | external                              （账单来的还是手记的）
sync             new | pushed | dirty | remote_changed | conflict
```

- `external` 的记录不参与映射，也不参与净额，仅在报表里作为"其他来源支出"展示
- `human_*` 永不被 `auto` 覆盖：规则或推荐器升级只重算 `auto` 的记录
- `approved` 与 `edited` 分开记，分别作为推荐器的正/负样本

## 4. 冷启动序列

```
1. 读 eml 目录 → 按 sha256 去重 → 解析（首次约 81ms/封，之后命中缓存）
2. 读规则日志 → 构建 description → (name, categoryFk) 映射
3. 逐笔推导 → 退款/消费金抵扣按商户池净额 → 得到"应然集合"
4. pull Cashew since=0（分页 2000）→ 得到"实然集合"
5. 对齐：
     bill-only    → 待推
     both         → 比对字段（§7），有差异则标 dirty 或采纳远端
     external     → 只读
6. 校验：应然笔数/金额合计 vs 实然笔数/金额合计，差异写告警日志
7. 进入稳态循环
```

第 6 步是必要的：没有它，一旦同步断裂（服务端 reset、客户端未推全量），我们会静默地少算一笔账而毫无察觉。

## 5. 稳态循环

```
定时（例如 5 分钟）或收到 /sync-stream 的 changed：
  pull 增量（since=游标）
    → 远端有变化的记录：更新本地实然值，按 §7 判定
    → 仅远端存在的记录：加入 external
  推送本批 dirty 记录（200 条/批）
    → 成功后更新推送指纹，状态置 pushed

收到新邮件（pull 服务触发）：
  解析新增（只解析新文件）
  → 推导 name/categoryFk
  → 净额重算（只涉及该商户池）
  → 立即推送（账面完整，不需要等人工）
  → 低置信项进入待审队列
```

**为什么先推后审**：账面从第一笔起完整；待审队列只影响"要不要人工看"，不影响账上有多少钱。

## 6. 写入契约

### 6.1 payload 生成（31 字段全表）

字段定义来自 fork 的 `app/lib/database/tables.dart`（`@DataClassName('Transaction')`）。类型注意：`amount` 是 **double**，日期是**毫秒整数**，枚举是**整数索引**。

状态图例：**业务** = 承载我们的语义；**固定** = 必须填但不表达业务；**待定** = 尚未决定。

| # | 字段 | 类型/可空 | 我们的取值 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | `transactionPk` | String 非空 | `record_id`（`src_…`），须等于 change 的 `pk` | 业务 |
| 2 | `pairedTransactionFk` | String 可空 | `null`（无转账配对） | 固定 |
| 3 | `name` | String 非空，**≤250** | 归一后的商户名。Cashew 无商户实体，**按 name 分组 = 统计口径** | 业务 |
| 4 | `amount` | double 非空 | 净额后的金额，负数=支出 | 业务 |
| 5 | `note` | String 非空，**≤500** | 原始 description + 抵消说明，截断到 200 字 | 业务 |
| 6 | `categoryFk` | String 非空 | 14 个分类的 pk（`"1"`–`"11"` 或 UUID，**按字符串处理**） | 业务 |
| 7 | `subCategoryFk` | String 可空 | `null`（暂未使用子类） | 固定 |
| 8 | `walletFk` | String 非空，默认 `"0"` | `"0"`（唯一钱包「银行」，cny） | 固定 |
| 9 | `dateCreated` | DateTime 非空 | 交易日（毫秒） | 业务 |
| 10 | `dateTimeModified` | DateTime 可空 | 递增毫秒，见 §6.2 | 固定 |
| 11 | `originalDateDue` | DateTime 可空 | 同 `dateCreated`（历史交易无应付日期概念） | 固定 |
| 12 | `income` | bool 非空 | `false`（消费）；未来收入记 `true` | 业务 |
| 13 | `periodLength` | int 可空 | `1`（客户端新增普通交易的默认值） | 固定 |
| 14 | `reoccurrence` | int 枚举可空<br>0=custom 1=daily 2=weekly **3=monthly** 4=yearly | `3`（monthly。客户端新增普通交易即写此值，非"重复"语义） | 固定 |
| 15 | `endDate` | DateTime 可空 | `null` | 固定 |
| 16 | `upcomingTransactionNotification` | bool 可空，默认 true | `true`（无实际影响） | 固定 |
| 17 | `type` | int 枚举可空<br>0=upcoming 1=subscription 2=repetitive 3=credit 4=debt | `null`（普通交易） | 固定 |
| 18 | `paid` | bool 非空，**表默认 false** | `true`（客户端新增普通交易写 true；默认 false 会被当成未支付） | 固定 |
| 19 | `createdAnotherFutureTransaction` | bool 可空，默认 false | `false` | 固定 |
| 20 | `skipPaid` | bool 非空，默认 false | `true`（客户端新增普通交易写 true） | 固定 |
| 21 | `methodAdded` | int 枚举可空<br>0=email 1=shared 2=csv 3=preview 4=appLink | `0`（email，标记机器导入）；客户端手工新增写 `null`。仅用于调试标签，不参与业务 | 固定 |
| 22 | `transactionOwnerEmail` | String 可空 | `null`（行级共享已移除，表定义保留仅为兼容） | 固定 |
| 23 | `transactionOriginalOwnerEmail` | String 可空 | `null` | 固定 |
| 24 | `sharedKey` | String 可空 | `null` | 固定 |
| 25 | `sharedOldKey` | String 可空 | `null` | 固定 |
| 26 | `sharedStatus` | int 枚举可空 | `null` | 固定 |
| 27 | `sharedDateUpdated` | DateTime 可空 | `null` | 固定 |
| 28 | `sharedReferenceBudgetPk` | String 可空 | `null` | 固定 |
| 29 | `objectiveFk` | String 可空 | `null`（不用目标/贷款） | 固定 |
| 30 | `objectiveLoanFk` | String 可空 | `null` | 固定 |
| 31 | `budgetFksExclude` | List&lt;String&gt; 可空 | `null`（试用；试点若客户端异常再改 `[]`） | 固定 |

**三个原待定项的结论（2026-10-29 用户确认）**

1. **`note` 保留原始 description**，截断到 200 字——这是唯一能回溯到账单的线索，字段上限 500 字留了余量
2. **`methodAdded = 0`（email）**，作为"机器导入"的粗过滤；手工记录保持 `null`。它只影响一个默认隐藏的调试标签，无业务副作用
3. **`budgetFksExclude = null`**，与表定义的可空性一致；试点阶段观察客户端是否报错，异常则改 `[]`

**长度约束**：`name ≤ 250`、`note ≤ 500`（`NAME_LIMIT` / `NOTE_LIMIT`），超出需截断，否则可能被客户端拒绝或截断出乱码。

### 6.2 时间戳策略

- 生成规则：`max(当前毫秒, 上一批最大值 + 1, 本地记录的最大值 + 1)`，同批内逐条递增
- **不得超过服务器时间 2 分钟**，否则被夹到服务器时间（实测于 API 文档）
- 旧时间戳会被判冲突并丢弃；同毫秒不同 payload 也会记冲突
- 因此每次推送前先用上一次 `push` 响应里的 `serverTime` 校准，避免客户端时钟漂移

## 6.3 推送方式：整行覆盖 + 排除集

用户量最多 2 人，几乎不会同时在 Cashew 与 mapping 前端改同一笔。因此**不做字段级合并，也不做乐观锁**：

- 审核产生的修改：整行覆盖，用新时间戳，`dateTimeModified` 的 LWW 天然解决先后
- 规则批量重推：跳过审核日志中标记为 `human_edited` 的 `record_id`，其余整行覆盖

排除集来自 `review-log.jsonl`（本来就要记），因此**不需要额外的推送指纹文件**。真撞上"两人同时改同一笔"时，结果是后写者赢，不丢数据，只是先写的那次被覆盖——可接受。

## 6.4 没有自定义字段，标记只能这么做

查过 `app/lib/database/tables.dart` 与迁移记录：**Cashew 没有留给用户的自定义字段**。

- `Labels` 表（标签）已被整段注释，并在迁移第 32 步 `deleteTable("Labels")` 删除；`labelFks` 列同样注释掉
- 行级共享字段（`sharedKey` 等）在新版只是为"schema 与上游逐字节一致"保留，无代码写入
- 客户端本地库实测 11 张表，没有 labels / tags / custom 之类的表

可用的自由文本只有两个：`name`（≤250）、`note`（≤500）。因此"标记"只有三种做法：

| 做法 | 可见性 | 评价 |
| --- | --- | --- |
| `note` 里加约定前缀（如 `[auto]`） | 用户在 Cashew 里能看到 | 污染显示，不推荐 |
| `methodAdded = 0`（email） | 默认不显示，需开调试开关才出现 "Added via: Email" | 语义正确、成本为零，**推荐** |
| 标记只留在本地日志（按 `record_id` 关联） | Cashew 里完全不可见 | 最干净，但查询要回我们这边 |

建议组合：`methodAdded = 0` 作为"机器导入"的粗过滤，细粒度状态（待审/已审/人工改过）留在 `review-log.jsonl`。不往 `note` 里塞标记。

## 7. 冲突处理

| 情形 | 动作 |
| --- | --- |
| 远端不存在 | 推送（新增） |
| 远端内容与推导一致 | 跳过 |
| 远端内容与推导不一致，且该笔在 `human_edited` 集合内 | 跳过，保留人工结果 |
| 远端内容与推导不一致，且不在排除集内 | 推送覆盖（LWW 新时间戳） |
| 远端 ID 不在应然集合（UUID） | 用户手记，只读，不参与任何写入 |
| 应然存在、远端缺失 | 推送补回 |

唯一需要人工介入的场景：同一笔`name` 在两边都被改过且结果不同。此时以 Cashew 现值展示，审核界面把我们的推导值作为"建议"列出，等人决定——不做自动裁决。

## 8. 持久化清单（最小集）

| 内容 | 形式 | 可否重建 |
| --- | --- | --- |
| 账单原件 | `evidence/cmb-email/<sha256>.eml` | 否（原始证据） |
| 人工决策 | `review-log.jsonl`（append-only） | **否**（唯一权威） |
| 映射表 | `merchants.yaml` / `categories.yaml` | 是（决策日志的投影） |
| 解析缓存 | `cache/<sha256>.json` | 是（纯派生，可删） |

只剩两样：原始证据与人工决策。推送不需要指纹文件（§6.3），同步游标丢了就从 `since=0` 重来。

## 9. 失败与幂等

- **推送幂等**：同 ID 同内容重推是 no-op；`conflictCount` 非零时逐条比对，不整批重试
- **部分失败**：按批提交，记录每批结果；失败的批次保持 dirty，下一轮重试
- **重复邮件**：sha256 相同直接跳过，不产生第二笔
- **规则重算**：只重算 `auto` 记录；`human_*` 与 `external` 不动
- **服务端 reset / 409 rebootstrap**：从 `minRetainedSeq` 重新拉取，并触发一次全量校验（§4 第 6 步）

## 10. 容量与性能（实测）

| 项目 | 1364 笔（当前） | 13.2 万笔（50 年高估） |
| --- | --- | --- |
| 内存 | ~1 MB | 63 MB |
| 全量净额 | 2.2 ms | 457 ms |
| 冷启动解析 | 1.1 s（14 封） | ~90 s（600 封） |
| 首推批次 | 7 批 | 597 批 |
| 单笔增量更新 | 0.32 µs | 0.32 µs |

**更正一个早期判断**：当前 1364 笔首推只需 7 批、几秒钟，Cashew 手机端毫无压力。所谓"容量策略"要到十万级才需要讨论，现在不必设计。

## 11. 未验证 / 待定

**未验证（不得假设）**

- `/sync/push` 是否会触发客户端的 `autoAddAssociatedTitles`（推断不会，因为走行级写入；需试点验证）
- 多条 `associated_titles` 同时命中时的优先级（`order`？最长匹配？）
- 服务端对非 ASCII `name` 的存储与回传是否无损
- 客户端遇到未知 `categoryFk` 时的行为（忽略还是报错）

**待定（需要你拍板）**

- 待审队列的置信阈值
- 无推荐时的兜底（用清理后的描述 + 哪个分类）
- 标注粒度：按 `description` 聚组，还是允许单条例外
- `note` 是否保留原始描述（我倾向保留，截断到 120 字）
- 是否把稳定的「商户名 → 分类」同步成 `associated_titles`（我倾向"只同步人工确认过的"，且默认精确匹配）

## 12. 落地顺序

1. **初始化同步**：把 14 个分类 + 1 个钱包推到服务端（服务端现在只有分类 `1`）
2. **只读对账**：`eml` 解析结果 ↔ `net-transactions.json`（已通过，1364 笔逐字段一致）
3. **试点导入 10 笔**：验证 payload 契约、时间戳规则、是否产生垃圾 associated titles、客户端显示是否正常
4. **全量导入 1364 笔**：7 批，逐批校验 `conflictCount=0`
5. **回读校验**：`pull since=0` 比对笔数、金额合计、分类分布
6. **接 IMAP 拉取服务**：oneshot + systemd timer，落 eml
7. **接审核界面**：待审队列 + 应用决策 + 推送

---

## 变更记录

| 时间 | 内容 |
| --- | --- |
| 2026-10-29 | 建立。确立三源对齐模型、字段级裁决、推送指纹必要性、容量更正 |
| 2026-10-29 | 简化（用户决策：≤2 人使用、几乎不会并发改同一笔）：取消字段级合并与乐观锁，改整行覆盖 + `human_edited` 排除集；删除 `push-state.json` |
