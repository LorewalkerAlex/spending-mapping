"""招行信用卡账单邮件解析。

把原始 EML 解析为逐笔交易。口径来自研究阶段并已对 14 封邮件做过 14/14 对账：

1. 交易类型来自「分节标题」（消费/退款/其他/还款/费用），不做启发式猜测
2. 每期必须通过对账恒等式：本期应还 − 上期应还 == Σ 全部行金额
3. 记录 ID 包含 EML 内位置（`sha256:<邮件哈希>` + HTML 分片哈希 + table 序号），
   因为存在同日同额同描述的多笔真实消费；重解析 ID 不变
4. 描述展示名仅用于界面，原始 description 始终保留

金额符号（原始账单口径）：消费为正、退款/其他/还款为负、费用可能为零。
Cashew 要「负数=支出」，所以消费在生成 payload 时取负——取负在净额/payload 环节，
不在解析环节。

依赖：beautifulsoup4（仅此一个第三方库，用于 HTML 解析）。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from email import message_from_bytes, policy
from pathlib import Path

from bs4 import BeautifulSoup

SOURCE_TYPE = "cmb_email"
CURRENCY = "CNY"
TARGET_WIDTH = "643"
TARGET_HEIGHT = "18"
DATE_TOLERANCE = timedelta(days=2)
MONEY = re.compile(r"^-?[\d,]+\.\d{2}$")
MMDD = re.compile(r"^\d{4}$")

CONSUMPTION = "消费"

# ---- 展示名规则（渠道前缀 + 法律实体后缀）----
CHANNEL_PREFIXES = (
    "消费金抵扣-商户红包-扫码-", "消费金抵扣-招财红包-扫码-", "消费金抵扣-银联加倍返现-扫码-",
    "消费金抵扣-", "掌上生活优惠商户-", "（特约）京东支付-", "云闪付扫码-", "云闪付-",
    "支付宝-", "财付通-", "京东支付-", "华为支付-", "中移动和包-", "扫码-",
)
LEGAL_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司", "个体工商户", "个体工商",
    "（个人独资）", "(个人独资)", "（个人", "(个人", "有限公", "有限责", "有限",
)
MIN_DISPLAY_LEN = 2


class ParseError(RuntimeError):
    """账单结构与预期不符时抛出，绝不静默降级。"""


def display_name(description: str) -> str:
    s = unicodedata.normalize("NFKC", description).strip()
    s = re.sub(r"^[（(]特约[）)]", "", s)
    changed = True
    while changed:
        changed = False
        for prefix in CHANNEL_PREFIXES:
            if s.startswith(prefix) and len(s) > len(prefix):
                s = s[len(prefix):].strip()
                changed = True
                break
    for suffix in LEGAL_SUFFIXES:
        if s.endswith(suffix) and len(s) - len(suffix) >= MIN_DISPLAY_LEN:
            s = s[: -len(suffix)]
            break
    return s if len(s) >= MIN_DISPLAY_LEN else description


def parse_money(text: str) -> Decimal:
    cleaned = text.replace("¥", "").replace(",", "").replace(" ", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", cleaned):
        raise ParseError(f"无法解析金额: {text!r}")
    return Decimal(cleaned)


def resolve_date(mmdd: str, period_start: date, period_end: date) -> date:
    """用账单周期推断年份；跨年时两个候选只有一个落在周期内。"""
    if MMDD.fullmatch(mmdd) is None:
        raise ParseError(f"非法日期: {mmdd!r}")
    month, day = int(mmdd[:2]), int(mmdd[2:])
    for year in (period_start.year, period_end.year):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if period_start - DATE_TOLERANCE <= candidate <= period_end + DATE_TOLERANCE:
            return candidate
    raise ParseError(f"日期 {mmdd} 不在账单周期 {period_start}~{period_end} 内")


@dataclass
class Record:
    record_id: str
    section: str
    txn_date: str
    post_date: str
    description: str
    display_name: str
    amount: str
    sign: str
    country: str
    is_consumption: bool

    @property
    def net_amount(self) -> Decimal:
        return Decimal(self.amount)


@dataclass
class Statement:
    sha256: str
    statement_date: str
    period_start: str
    period_end: str
    summary: dict
    reconcile: dict
    section_totals: dict
    records: list[Record] = field(default_factory=list)


def html_parts(raw: bytes) -> list[str]:
    msg = message_from_bytes(raw, policy=policy.default)
    parts, seen = [], defaultdict(int)
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() != "text/html":
            continue
        html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
        seen[hashlib.sha256(html.encode("utf-8")).hexdigest()] += 1
        parts.append(html)
    return parts


def part_key(html: str, ordinal: int) -> str:
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    return f"html:{digest}:{ordinal}"


def record_id(evidence_identity: str, locator: str) -> str:
    payload = f"source-record\0{SOURCE_TYPE}\0{evidence_identity}\0{locator}".encode()
    return "src_" + hashlib.sha256(payload).hexdigest()[:24]


def parse_statement(path: Path) -> Statement:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    evidence_identity = f"sha256:{digest}"

    parts = html_parts(raw)
    if len(parts) != 1:
        raise ParseError(f"{path.name}: 期望 1 个 HTML 分片，实际 {len(parts)}")

    soup = BeautifulSoup(parts[0], "html.parser")
    key = part_key(parts[0], 1)

    period = _find_period(soup)
    summary = _find_summary(soup)
    period_start, period_end = period

    records: list[Record] = []
    current_section = None
    for ordinal, table in enumerate(soup.find_all("table"), start=1):
        if (str(table.get("width")), str(table.get("height"))) != (TARGET_WIDTH, TARGET_HEIGHT):
            continue
        row = table.find("tr")
        if not row:
            continue
        cells = row.find_all("td", recursive=False)
        values = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in cells]

        if len(cells) == 3:
            if values[1]:
                current_section = values[1]
            continue
        if len(cells) != 8:
            continue
        if current_section is None:
            raise ParseError(f"{path.name}: 交易行出现在分节标题之前（table #{ordinal}）")

        txn_mmdd, post_mmdd, desc, amount_text = values[1], values[2], values[3], values[4]
        if not desc:
            raise ParseError(f"{path.name}: 描述为空（table #{ordinal}）")

        # 还款行的交易日为空，用入账日占位；分节已给出类型，不需要再猜
        date_source = txn_mmdd or post_mmdd
        txn = resolve_date(date_source, period_start, period_end)
        post = resolve_date(post_mmdd, period_start, period_end) if post_mmdd else txn
        amount = parse_money(amount_text)

        records.append(Record(
            record_id=record_id(evidence_identity, f"{key}/table:{ordinal}"),
            section=current_section,
            txn_date=txn.isoformat(),
            post_date=post.isoformat(),
            description=desc,
            display_name=display_name(desc),
            amount=str(amount),
            sign="负" if amount < 0 else ("零" if amount == 0 else "正"),
            # 第 5 格是卡号后四位。按确认的口径不作为统计维度（家庭共同消费），
            # 且属敏感信息，不进结构化输出；需要时从原始 EML 重读。
            country=values[6],
            is_consumption=current_section == CONSUMPTION,
        ))

    if not records:
        raise ParseError(f"{path.name}: 未解析到任何交易行")

    section_totals: dict[str, Decimal] = defaultdict(Decimal)
    for r in records:
        section_totals[r.section] += Decimal(r.amount)

    reconcile = _reconcile(summary, section_totals, records)

    return Statement(
        sha256=digest,
        statement_date=(period_end + timedelta(days=1)).isoformat(),
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        summary={k: str(v) for k, v in summary.items()},
        reconcile=reconcile,
        section_totals={k: str(v) for k, v in section_totals.items()},
        records=records,
    )


def parse_directory(directory: Path) -> list[Statement]:
    """解析一个目录下所有 .eml，按文件名排序。"""
    return [parse_statement(path) for path in sorted(directory.glob("*.eml"))]


def _find_period(soup: BeautifulSoup) -> tuple[date, date]:
    pattern = re.compile(r"(\d{4})/(\d{2})/(\d{2})-(\d{4})/(\d{2})/(\d{2})")
    for table in soup.find_all("table")[:40]:
        match = pattern.search(table.get_text(" ", strip=True))
        if match:
            y1, m1, d1, y2, m2, d2 = (int(x) for x in match.groups())
            return date(y1, m1, d1), date(y2, m2, d2)
    raise ParseError("未找到账单周期")


SUMMARY_KEYS = ("due", "previous", "paid", "spend", "refund", "fee")


def _find_summary(soup: BeautifulSoup) -> dict:
    """汇总块标签是图片，只能按固定顺序取首个分节标题前的最后 6 个金额。"""
    tokens = [
        el.strip().replace("¥", "").replace(" ", "")
        for el in soup.descendants
        if isinstance(el, str)
    ]
    tokens = [t for t in tokens if t]
    cut = next((i for i, t in enumerate(tokens) if t == "还款"), len(tokens))
    money = [t for t in tokens[:cut] if MONEY.match(t)]
    if len(money) < 6:
        raise ParseError(f"汇总块金额不足 6 项，实际 {len(money)}")
    return {k: parse_money(v) for k, v in zip(SUMMARY_KEYS, money[-6:])}


def _reconcile(summary: dict, totals: dict[str, Decimal], records: list[Record]) -> dict:
    total = sum((Decimal(r.amount) for r in records), Decimal(0))
    identity_delta = (summary["due"] - summary["previous"]) - total
    checks = {
        "identity_ok": identity_delta == 0,
        "identity_delta": str(identity_delta),
        # 银行把「其他」并入退款汇总项
        "refund_ok": abs(totals.get("退款", Decimal(0)) + totals.get("其他", Decimal(0)))
        == summary["refund"],
        "spend_ok": totals.get("消费", Decimal(0)) == summary["spend"],
        "paid_ok": abs(totals.get("还款", Decimal(0))) == summary["paid"],
        "fee_ok": totals.get("费用", Decimal(0)) == summary["fee"],
    }
    checks["all_ok"] = all(v for k, v in checks.items() if k.endswith("_ok"))
    return checks
