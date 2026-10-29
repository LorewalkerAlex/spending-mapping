# Cashew 同步 API 接入说明

面向 Mapping 系统读写 Cashew 数据。以下结论来源分三档，逐条标注：

- **实测**：2026-10-27 ~ 10-29 对 `ghcr.io/vfyuj/cashew-selfhosted:1.4.3` 的真实调用
- **源码**：fork 仓库 `docs/server/api.md`、`docs/server/sync.md`、`app/lib/database/tables.dart`
- **未验证**：尚未确认，接入前需自行验证

最后更新：2026-10-29。

---

## 1. 部署与访问地址

单容器 Dart/Shelf 服务，SQLite 落盘 `/srv/persistent/data/cashew/db.sqlite`，容器仅绑回环 `127.0.0.1:13004`，对外由 Caddy 反代。

| 用途 | 基地址 | 说明 |
| --- | --- | --- |
| 同机其它服务（推荐） | `http://127.0.0.1:13004` | 跳过 TLS 与反代 |
| 内网客户端 | `https://cashew.eurexis.com:4129` | LAN 站点 |
| 公网 | `https://cashew.eurexis.com` | 443，同域名不同端口 |

---

## 2. 认证

### 2.1 端点（源码）

| 路由 | 鉴权 | 说明 |
| --- | --- | --- |
| `POST /auth/login` | 无 | `{email, password}` → session，401 凭据错误 |
| `POST /auth/refresh` | token 在 body | `{sessionToken}` → 新的 `{sessionToken, expiresAt}`，**滑动续期**，过期 401 |
| `POST /auth/logout` | token 在 body | 总是 200 |
| `GET /auth/me` | session | 当前账号 |
| `GET /auth/setup-state` | 无 | `{needsSetup}` 是否尚无账号 |
| `POST /auth/setup` | 无 | 建首个管理员，已存在则 409 |

登录成功（实测）：

```json
{ "sessionToken": "…", "expiresAt": "…", "user": { "email": "…", "isAdmin": true } }
```

`sessionToken` 有效期 30 天，**有 `/auth/refresh` 可滑动续期**，不需要重新输密码。

### 2.2 携带方式

同步接口带 `Authorization: Bearer <sessionToken>`。`/sync-stream` 例外：WebSocket 首帧发 `{"type":"auth","token":"…"}`，不能放 URL（会进反代日志）。

---

## 3. 同步模型（这是最容易误解的部分）

**服务端是变更中继，不是数据仓库。** `sync_records` 表**每行一条记录、只存最新版本**，不是每条变更一行。因此：

- `pull?since=0` 返回的是"客户端推送过的所有当前行"，等价于服务端已知的全量
- 服务端没有全表快照接口（1.3.x 已移除 `/sync/files*`）
- **客户端本地 SQLite 才是全量**；服务端只持有被推过的行
- 完整数据通过 `GET /backup/list` + `GET /backup/<filename>` 取备份（备份是本地库原件）

### 3.1 拉取

```
GET /sync/pull?since=<seq>&limit=<n>
```

- `limit` 默认 500，夹到 1–2000
- `since` 低于 `min_retained_seq` → **`409 {"error":"rebootstrap","minRetainedSeq":<n>}`**，须从该值重拉并重推
- 响应：

```json
{
  "changes": [
    { "seq": 11, "table": "Transaction", "pk": "…", "deleted": false,
      "modifiedAt": 1793152282135, "payload": { "…": "…" } }
  ],
  "nextCursor": 12, "hasMore": false, "serverTime": 1793152282135
}
```

### 3.2 推送

```
POST /sync/push
{ "deviceId": "…", "changes": [ { "table", "pk", "deleted", "modifiedAt", "payload" } ] }
```

响应：`{ "serverTime": <ms>, "conflictCount": <n> }`

**LWW 判定（源码）**：

| 入站 vs 已存 | 结果 |
| --- | --- |
| 无已存行 | insert，`appliedCount++` |
| 更新 | update |
| 同时间戳、同 payload | no-op（重试或回显） |
| 同时间戳、不同 payload | `conflictCount++`（真冲突） |
| 更旧 | `conflictCount++`，丢弃 |

`modifiedAt` **超前服务器时间 2 分钟会被夹到服务器时间**。写脚本时先用 push 响应里的 `serverTime` 校准，避免客户端时钟漂移。

### 3.3 其它同步端点

- `POST /sync/reset`：清空 `sync_records`，`min_retained_seq` 顶到 `next_seq`，所有设备下次 pull 得 409 重推
- `GET /sync-stream`：WebSocket 唤醒，服务端只发 `{"type":"changed"}`（不带数据），客户端收到后普通 pull

---

## 4. 表与字段

### 4.1 同步表名（camelCase，实测）

| 同步表 | 本地库表（snake_case） | 说明 |
| --- | --- | --- |
| `Transaction` | `transactions` | 交易 |
| `TransactionCategory` | `categories` | 分类 |
| `TransactionAssociatedTitle` | `associated_titles` | 关联标题（标题→分类规则） |

本地库还有 `wallets`、`budgets`、`objectives`、`app_settings`、`delete_logs` 等；同步侧观察到的只有上面三张。**写同步用 camelCase，读备份用 snake_case，不可混。**

### 4.2 Transaction payload（31 字段）

类型要点：`amount` 是 **double**，日期是**毫秒整数**，枚举是**整数索引**，`name ≤250`、`note ≤500`。

取值见 `docs/PIPELINE-DESIGN.md §6.1`，这里只列最易错的部分：

| 字段 | 类型 | 我们的取值 |
| --- | --- | --- |
| `transactionPk` | string | `record_id`（`src_…`），等于 change `pk` |
| `amount` | double | 负数=支出，正数=收入 |
| `income` | bool | 只影响展示归类，不改变聚合方向 |
| `categoryFk` | string | 分类 pk，`"1"`–`"11"` 或 UUID，**当字符串** |
| `walletFk` | string | `"0"` = 唯一钱包「银行」 |
| `type` | int 枚举可空 | `null`（普通交易） |
| `paid` | bool | `true`（表默认 false） |
| `skipPaid` | bool | `true`（客户端新增普通交易即写 true） |
| `reoccurrence` | int | `3`（monthly；客户端默认，非"重复"语义） |
| `periodLength` | int | `1` |
| `methodAdded` | int 可空 | `0`（email，标记机器导入） |

### 4.3 枚举（源码，成员顺序即整数值）

```
BudgetReoccurence:      0=custom 1=daily 2=weekly 3=monthly 4=yearly
TransactionSpecialType: 0=upcoming 1=subscription 2=repetitive 3=credit 4=debt
MethodAdded:            0=email 1=shared 2=csv 3=preview 4=appLink
SharedStatus:           0=waiting 1=shared 2=error
```

### 4.4 分类与钱包现状（实测，来自客户端备份）

- 分类 14 个：默认 `1`–`11`（三餐/杂货/购物/交通/娱乐/定期账单与费用/礼物/美妆/工作/旅行/收入），用户新建 3 个为 UUID（医疗/运动/其他）
- 钱包 1 个：`wallet_pk="0"`「银行」，cny，2 位小数

---

## 5. 实测语义（已验证）

1. **主键端到端原样保留**（19/19）：`change.pk` / `payload.transactionPk` / `amount` / `name` / `deleted` 均不被改写，`src_`+24hex 与 UUID 都可用
2. **金额符号即方向**：负数恒计入支出、正数恒计入收入；`income` 只影响展示
3. **服务端不校验 payload**：缺字段/类型错都原样入库，客户端渲染时才暴露——校验责任在写入方
4. **幂等**：同主键重复推是 upsert，不产生第二条

---

## 6. 未验证项

- `/sync/push` 是否触发客户端 `autoAddAssociatedTitles`（推断不会，行级写入不走 UI 逻辑）
- 多条 `associated_titles` 命中时的优先级
- 服务端对非 ASCII `name` 的往返是否无损
- 客户端遇到未知 `categoryFk` 时的行为
- `push` 单批条数上限与超时行为
- `budgetFksExclude` 的 `null` 与 `[]` 在 `StringListInColumnConverter` 下是否等价

---

## 7. 变更记录

| 时间 | 内容 |
| --- | --- |
| 2026-10-28 | 建立，收录首轮实测 |
| 2026-10-29 | 依据 `docs/server/api.md`、`sync.md`、`tables.dart` 与客户端备份重写：改为"变更中继"模型、补 refresh/reset/stream、更正表名与字段类型、更正 wallet 与枚举值 |
