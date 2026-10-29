"""把导入集的一笔交易转成 Cashew 的 Transaction payload。

31 个字段的取值依据见 docs/PIPELINE-DESIGN.md §6.1，关键结论：
  - amount 是 double，负数表示支出
  - 日期是毫秒整数（本服务按 +08:00 取当日零点）
  - name ≤ 250、note ≤ 500（客户端 NAME_LIMIT / NOTE_LIMIT）
  - 普通一次性支出：paid=true, skipPaid=true, reoccurrence=3, periodLength=1, type=null
    （与客户端新增普通交易时的写入值一致）
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from .records import ImportTransaction

NAME_LIMIT = 250
NOTE_LIMIT = 500
NOTE_TRUNCATE = 200
LOCAL_TZ = timezone(timedelta(hours=8))

# MethodAdded 枚举：0=email 1=shared 2=csv 3=preview 4=appLink
METHOD_ADDED_EMAIL = 0


def date_to_ms(date_text: str) -> int:
    """`YYYY-MM-DD` → 当日 00:00（+08:00）的毫秒时间戳。

    假设客户端在东八区渲染 `dateCreated`：Cashew 存的是绝对毫秒，客户端按设备时区
    显示日期。若未来出现非东八区设备，这里的"当日零点"会偏一天，需改成按设备时区
    或干脆存中午 12:00 规避跨日边界。当前家庭设备均在 +08:00，此假设成立。
    """
    day = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    return int(day.timestamp() * 1000)


def build_note(txn: ImportTransaction, limit: int = NOTE_TRUNCATE) -> str:
    text = txn.description or txn.note or txn.merchant
    if txn.offset > 0:
        text = f"{text} | 原价 {txn.gross} 已抵消 {txn.offset}"
    return text[:limit]


def build_payload(
    txn: ImportTransaction,
    *,
    category_pk: str,
    wallet_pk: str = "0",
    modified_ms: int,
    method_added: int | None = METHOD_ADDED_EMAIL,
    income: bool = False,
    name: str | None = None,
) -> dict:
    created_ms = date_to_ms(txn.date)
    resolved_name = name if name is not None else txn.merchant
    name = resolved_name[:NAME_LIMIT]
    note = build_note(txn)[:NOTE_LIMIT]
    return {
        "transactionPk": txn.source_record_id,
        "pairedTransactionFk": None,
        "name": name,
        "amount": float(txn.amount),
        "note": note,
        "categoryFk": category_pk,
        "subCategoryFk": None,
        "walletFk": wallet_pk,
        "dateCreated": created_ms,
        "dateTimeModified": modified_ms,
        "originalDateDue": created_ms,
        "income": income,
        "periodLength": 1,
        "reoccurrence": 3,
        "endDate": None,
        "upcomingTransactionNotification": True,
        "type": None,
        "paid": True,
        "createdAnotherFutureTransaction": False,
        "skipPaid": True,
        "methodAdded": method_added,
        "transactionOwnerEmail": None,
        "transactionOriginalOwnerEmail": None,
        "sharedKey": None,
        "sharedOldKey": None,
        "sharedStatus": None,
        "sharedDateUpdated": None,
        "sharedReferenceBudgetPk": None,
        "objectiveFk": None,
        "objectiveLoanFk": None,
        "budgetFksExclude": None,
    }


def build_change(payload: dict, modified_ms: int) -> dict:
    """包成 /sync/push 的 change；pk 必须与 payload.transactionPk 一致。"""
    return {
        "table": "Transaction",
        "pk": payload["transactionPk"],
        "deleted": False,
        "modifiedAt": modified_ms,
        "payload": payload,
    }


def payload_digest(payload: dict) -> str:
    """内容指纹，用于判断"这笔内容变没变"。

    排除 `dateTimeModified`：它是我们写入时的钟点，不属于业务内容。
    若把它算进去，同一笔内容每次推送都会得到不同指纹，"是否需要推"就永远为真。
    """
    content = {k: v for k, v in payload.items() if k != "dateTimeModified"}
    canon = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
