"""审核前端：一个单页 Web UI + JSON 接口。

绑定 127.0.0.1（宿主机回环），通过 SSH 端口转发访问，不需要 Caddy/PAC/UFW 变更。

接口：
  GET  /                    审核页
  GET  /api/pending         待审分类 + 待审退款（含候选抵消目标）
  POST /api/apply-categories {decisions:[{name, category_fk}]}
  POST /api/apply-refund     {record_id, action:"income"|"offset", category_fk?, target_record_id?}
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .cashew_client import CashewClient
from .categories import Category, load_categories
from .parser import Statement, display_name, parse_directory
from .review import (
    apply_categories,
    apply_refund_income,
    apply_refund_offset,
    list_pending_categories,
    list_pending_refunds,
)
from .review_log import ReviewLog
from .suggest import suggest

IMPORTED_PREFIX = "src_"


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def _evidence_dir() -> Path:
    override = os.environ.get("EVIDENCE_DIR")
    return Path(override) if override else _data_dir() / "evidence" / "cmb-email"


def default_client() -> CashewClient:
    return CashewClient(
        os.environ.get("CASHEW_BASE", "http://127.0.0.1:13004"),
        session_file=_data_dir() / ".cashew-session.json",
    )


def _refund_candidates(statements: list[Statement], refund, limit: int = 12) -> list[dict]:
    """同商户、日期不晚于退款日的已入账消费，供选择抵消目标。"""
    from .netting import net_balance

    records = [record for statement in statements for record in statement.records]
    netted = net_balance(records)
    name = refund.merchant
    candidates = []
    for spend in netted.spends:
        if spend.record.display_name != name:
            continue
        if spend.record.txn_date > refund.date:
            continue
        candidates.append({
            "record_id": spend.record.record_id,
            "date": spend.record.txn_date,
            "amount": str(-spend.remaining),
        })
    candidates.sort(key=lambda item: item["date"], reverse=True)
    return candidates[:limit]


class ReviewServer(ThreadingHTTPServer):
    def __init__(self, address, client_factory=default_client):
        super().__init__(address, Handler)
        self.client_factory = client_factory
        self.data_dir = _data_dir()
        self.evidence_dir = _evidence_dir()

    def client(self) -> CashewClient:
        return self.client_factory()

    def log(self) -> ReviewLog:
        return ReviewLog.load(self.data_dir / "review-log.jsonl")

    def statements(self) -> list[Statement]:
        return parse_directory(self.evidence_dir)


class Handler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, *args):  # noqa: ANN002 — 静音访问日志
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/pending":
            self._pending()
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/apply-categories":
                self._apply_categories(body)
            elif path == "/api/apply-refund":
                self._apply_refund(body)
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 — 前端要把错误显示出来
            self._json(400, {"error": str(exc)})

    def _pending(self) -> None:
        categories: list[Category] = load_categories()
        client = self.server.client()
        merchants = list_pending_categories(client, self.server.log())
        statements = self.server.statements()
        refunds = list_pending_refunds(statements, self.server.log())

        payload = {
            "categories": [{"pk": c.pk, "name": c.name} for c in categories],
            "merchants": [
                {
                    "name": item.name,
                    "count": item.count,
                    "total": str(item.total),
                    "sample_note": item.sample_note,
                    "suggested_fk": suggest(item.name, categories),
                }
                for item in merchants
            ],
            "refunds": [
                {
                    "record_id": item.record_id,
                    "date": item.date,
                    "amount": str(item.amount),
                    "merchant": item.merchant,
                    "description": item.description,
                    "candidates": _refund_candidates(statements, item),
                }
                for item in refunds
            ],
        }
        self._json(200, payload)

    def _apply_categories(self, body: dict) -> None:
        decisions = {str(d["name"]): str(d["category_fk"]) for d in body.get("decisions", [])}
        changed = apply_categories(self.server.client(), decisions, self.server.log())
        self._json(200, {"applied": len(decisions), "changed": changed})

    def _apply_refund(self, body: dict) -> None:
        record_id = str(body["record_id"])
        action = str(body["action"])
        statements = self.server.statements()
        client = self.server.client()
        if action == "income":
            apply_refund_income(client, statements, record_id, str(body["category_fk"]), self.server.log())
        elif action == "offset":
            apply_refund_offset(client, statements, record_id, str(body["target_record_id"]), self.server.log())
        else:
            raise ValueError(f"未知动作 {action!r}")
        self._json(200, {"ok": True})


HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>账单审核</title>
<style>
:root{--bg:#12141a;--panel:#1b1f28;--line:#2c323f;--fg:#e6e9ef;--dim:#9aa4b2;--accent:#5b8cff;--ok:#3fb950;--warn:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:16px;margin:0;font-weight:600}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
button:disabled{opacity:.5;cursor:default}
.tabs{display:flex;gap:6px;margin-left:12px}
.tab{padding:6px 12px;border-radius:6px;cursor:pointer;color:var(--dim);border:1px solid transparent}
.tab.active{color:var(--fg);border-color:var(--line);background:var(--panel)}
main{padding:14px 18px}
input[type=search],select{background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:13px}
table{width:100%;border-collapse:collapse;margin-top:10px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:middle}
th{color:var(--dim);font-weight:500;position:sticky;top:57px;background:var(--bg)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
tr.done{opacity:.45}
.badge{font-size:11px;color:var(--warn);border:1px solid var(--warn);border-radius:4px;padding:0 4px;margin-left:6px}
.hint{color:var(--dim);font-size:12px}
.row{display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.err{color:#f85149}
</style></head><body>
<header>
  <h1>账单审核</h1>
  <button id="load">开始审核</button>
  <div class="tabs">
    <div class="tab active" data-tab="cat">分类 <span id="ncat"></span></div>
    <div class="tab" data-tab="ref">退款 <span id="nref"></span></div>
  </div>
  <span class="hint" id="msg"></span>
</header>
<main>
  <section id="cat-panel">
    <div class="row">
      <input type="search" id="q" placeholder="搜索商户名…" style="width:240px">
      <button class="ghost" id="applyAll">应用所有已选</button>
      <span class="hint">建议值已预选，可改；应用后立即写回 Cashew。</span>
    </div>
    <table><thead><tr><th>商户</th><th>笔数</th><th class="num">金额</th><th>分类</th><th></th></tr></thead>
    <tbody id="cat-body"></tbody></table>
  </section>
  <section id="ref-panel" hidden>
    <p class="hint">未匹配到消费的退款：可以指定抵消某笔消费，或直接记为收入。</p>
    <table><thead><tr><th>日期</th><th class="num">金额</th><th>商户</th><th>处理</th></tr></thead>
    <tbody id="ref-body"></tbody></table>
  </section>
</main>
<script>
let CATS=[], MERCH=[], REFS=[], tab='cat';
const $=s=>document.querySelector(s);
const money=v=>Number(v).toFixed(2);

function opts(sel){
  return CATS.map(c=>`<option value="${c.pk}" ${c.pk===sel?'selected':''}>${c.name}</option>`).join('');
}
function renderCat(){
  const q=$('#q').value.trim().toLowerCase();
  const rows=MERCH.filter(m=>!q||m.name.toLowerCase().includes(q));
  $('#cat-body').innerHTML=rows.map(m=>{
    const idx=MERCH.indexOf(m);
    return `<tr data-i="${idx}">
      <td>${m.name}${m.suggested_fk?'<span class="badge">建议</span>':''}</td>
      <td class="num">${m.count}</td>
      <td class="num">${money(m.total)}</td>
      <td><select class="sel">${opts(m.suggested_fk||'')}</select></td>
      <td><button class="apply" ${m.suggested_fk?'':'disabled'}>应用</button></td>
    </tr>`;
  }).join('');
  $('#ncat').textContent=`(${MERCH.length})`;
}
function renderRef(){
  $('#ref-body').innerHTML=REFS.map(r=>{
    const cand=r.candidates.map(c=>`<option value="${c.record_id}">${c.date} ${money(c.amount)}</option>`).join('');
    return `<tr data-id="${r.record_id}">
      <td>${r.date}</td><td class="num">${money(r.amount)}</td><td>${r.merchant}</td>
      <td>
        ${r.candidates.length?`<select class="cand">${cand}</select><button class="off">抵消所选</button>`:''}
        <select class="inc">${opts('')}</select><button class="incbtn">转收入</button>
      </td>
    </tr>`;
  }).join('');
  $('#nref').textContent=`(${REFS.length})`;
}
async function post(path,body){
  const res=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await res.json();
  if(!res.ok) throw new Error(data.error||'请求失败');
  return data;
}
async function load(){
  $('#msg').textContent='加载中…';
  const res=await fetch('/api/pending'); const data=await res.json();
  CATS=data.categories; MERCH=data.merchants; REFS=data.refunds;
  renderCat(); renderRef();
  $('#msg').textContent=`待审 ${MERCH.length} 商户 / ${REFS.length} 退款`;
}
$('#load').onclick=load;
$('#q').oninput=renderCat;
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  tab=t.dataset.tab;
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===t));
  $('#cat-panel').hidden = tab!=='cat';
  $('#ref-panel').hidden = tab!=='ref';
});
$('#cat-body').addEventListener('click',async e=>{
  if(!e.target.classList.contains('apply')) return;
  const tr=e.target.closest('tr'); const i=+tr.dataset.i; const fk=tr.querySelector('.sel').value;
  e.target.disabled=true;
  try{
    const r=await post('/api/apply-categories',{decisions:[{name:MERCH[i].name,category_fk:fk}]});
    tr.classList.add('done'); e.target.textContent='已应用';
  }catch(err){ e.target.disabled=false; alert(err.message); }
});
$('#applyAll').onclick=async ()=>{
  const decisions=[];
  document.querySelectorAll('#cat-body tr').forEach(tr=>{
    const sel=tr.querySelector('.sel'); if(sel&&sel.value) decisions.push({name:MERCH[+tr.dataset.i].name,category_fk:sel.value});
  });
  if(!decisions.length){ alert('没有已选分类的行'); return; }
  $('#msg').textContent=`应用 ${decisions.length} 条…`;
  try{
    const r=await post('/api/apply-categories',{decisions});
    $('#msg').textContent=`已应用 ${r.applied} 个商户，改动 ${r.changed} 笔`;
    MERCH=MERCH.filter(m=>!decisions.some(d=>d.name===m.name)); renderCat();
  }catch(err){ $('#msg').textContent=err.message; }
};
$('#ref-body').addEventListener('click',async e=>{
  const tr=e.target.closest('tr'); const id=tr.dataset.id;
  try{
    if(e.target.classList.contains('off')){
      const target=tr.querySelector('.cand').value;
      await post('/api/apply-refund',{record_id:id,action:'offset',target_record_id:target});
      tr.classList.add('done'); $('#msg').textContent='已抵消';
    }
    if(e.target.classList.contains('incbtn')){
      const fk=tr.querySelector('.inc').value;
      await post('/api/apply-refund',{record_id:id,action:'income',category_fk:fk});
      tr.classList.add('done'); $('#msg').textContent='已记为收入';
    }
  }catch(err){ alert(err.message); }
});
</script></body></html>
"""


def serve(host: str = "127.0.0.1", port: int = 13010) -> None:
    server = ReviewServer((host, port))
    print(f"审核页：http://{host}:{port}（SSH 端口转发后本地打开）")
    server.serve_forever()
