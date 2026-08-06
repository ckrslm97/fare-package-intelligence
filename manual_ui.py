"""Manuel giriş editörü — http://localhost:8766

Paketleri tarayıcıdan ekle, düzelt, sil. Yayınlanan pano statik bir sayfadır ve
diske yazamaz; bu yüzden editör küçük bir YEREL sunucu olarak çalışır ve
girdileri doğrudan ``manual_data/entries.csv`` dosyasına yazar — Excel'e gidip
gelmeden. Doğrulama, hak devralma ve birleştirme mantığı ``manual.py`` ile
AYNI koddur (buradan import edilir), yani iki giriş yolu asla ayrışmaz.

Kullanım:
    python manual_ui.py                       # en güncel output_* klasörünü açar
    python manual_ui.py output_v2_20260805-034945
"""
from __future__ import annotations

import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from branded_fare_scraper.amenities import AMENITY_DISPLAY  # noqa: E402
from branded_fare_scraper.rebuild import iter_raw_records  # noqa: E402
from manual import (COLUMNS, RIGHT_COLS, read_store, validate,  # noqa: E402
                    write_store, brand_match_key)

PORT = 8766
DATA: dict = {"units": {}, "dir": ""}


def pick_dir() -> Path:
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        if not (p / "raw_data.jsonl").exists():
            raise SystemExit(f"{p}/raw_data.jsonl yok")
        return p
    cands = [p.parent for p in ROOT.glob("output_*/raw_data.jsonl")]
    if not cands:
        raise SystemExit("output_*/raw_data.jsonl bulunamadı; klasörü argüman olarak verin")
    return max(cands, key=lambda p: (p / "raw_data.jsonl").stat().st_mtime)


def load(out_dir: Path) -> None:
    """Read the scrape once into memory, keyed by unit, so the page can ask
    for one unit at a time instead of shipping the whole dataset."""
    units: dict[str, dict] = {}
    for rec in iter_raw_records(out_dir / "raw_data.jsonl"):
        key = "|".join([rec.get("carrier", ""), rec.get("origin", ""),
                        rec.get("destination", ""), rec.get("season", "")])
        cabins = {}
        for c in rec.get("cabins", []):
            pkgs = []
            for i, b in enumerate(c.get("brands", []), start=1):
                rights = {}
                for a in b.get("amenities", []):
                    disp = AMENITY_DISPLAY.get(a.get("canonical_key") or "")
                    if disp:
                        rights[disp] = {"state": a.get("status", ""),
                                        "detail": (a.get("raw_value") or "").strip()}
                pkgs.append({"order": i, "name": b.get("raw_brand_name", ""),
                             "price": b.get("price_value"), "rights": rights,
                             "manual": b.get("source") == "manual"})
            if pkgs:
                cabins[c.get("cabin", "")] = pkgs
        if cabins:
            units[key] = cabins
    DATA["units"] = units
    DATA["dir"] = str(out_dir)


def index() -> dict:
    """carrier -> ond -> [season] , plus the cabins each unit actually has."""
    tree: dict = {}
    for key, cabins in DATA["units"].items():
        car, o, d, season = key.split("|")
        tree.setdefault(car, {}).setdefault(f"{o}-{d}", {})[season] = list(cabins)
    return tree


PAGE = r"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FPI — Manuel Giriş</title><style>
:root{--bg:#F6F4F2;--panel:#fff;--ink:#1A1614;--dim:#6B6360;--faint:#938C88;
 --line:#E4DEDB;--line2:#D3CBC7;--tk:#B7312C;--tk-soft:#FBF0EF;--ok:#0C7A4D;--ok-soft:#E6F5EE;
 --warn:#8A5A00;--warn-soft:#FDF6E9;
 --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
 --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:14px 22px;
 display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:20}
header b{font-size:15px}header .sub{color:var(--faint);font-size:12px}
.wrap{max-width:1180px;margin:0 auto;padding:20px 22px 90px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}
h2{font:600 11px/1 var(--sans);letter-spacing:.12em;text-transform:uppercase;color:var(--faint);
 margin:0 0 14px;padding-bottom:9px;border-bottom:1px solid var(--line)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.f{display:flex;flex-direction:column;gap:5px}
.f label{font:700 10px/1 var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}
select,input{font:13px var(--sans);border:1px solid var(--line2);border-radius:8px;
 padding:0 10px;height:34px;background:#fff;color:var(--ink)}
select:focus,input:focus{outline:none;border-color:var(--tk);box-shadow:0 0 0 3px rgba(183,49,44,.13)}
input.num{width:110px;font-family:var(--mono);text-align:right}
button{font:600 13px var(--sans);border:1px solid var(--line2);background:#fff;color:var(--ink);
 border-radius:8px;height:34px;padding:0 14px;cursor:pointer}
button:hover{border-color:var(--tk);color:var(--tk)}
button.primary{background:var(--tk);border-color:var(--tk);color:#fff}
button.primary:hover{filter:brightness(1.08);color:#fff}
button.ghost{border-style:dashed}
button:disabled{opacity:.45;cursor:not-allowed}
table{width:100%;border-collapse:collapse}
th{font:700 10px/1 var(--sans);letter-spacing:.07em;text-transform:uppercase;color:var(--faint);
 text-align:left;padding:0 8px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid #F2EEEC;vertical-align:middle}
tr.rm td{opacity:.42;text-decoration:line-through}
tr.new td{background:#FBFCFF}
.pill{display:inline-block;font:700 10px/1 var(--sans);letter-spacing:.05em;text-transform:uppercase;
 padding:4px 8px;border-radius:99px;border:1px solid}
.p-src{color:var(--dim);border-color:var(--line2);background:#FAF8F7}
.p-man{color:var(--tk);border-color:#EBC0BD;background:var(--tk-soft)}
.p-new{color:var(--ok);border-color:#A9D9C4;background:var(--ok-soft)}
.rights{background:#FBFAF9;border:1px solid var(--line);border-radius:10px;padding:14px;margin-top:4px}
.rgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:10px}
.r{display:grid;grid-template-columns:1fr 116px 1fr;gap:6px;align-items:center}
.r span{font-size:12.5px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.r select{height:30px;font-size:12px;padding:0 6px}
.r input{height:30px;font-size:12px}
.hint{font-size:12.5px;color:var(--dim);margin:-6px 0 14px}
.bar{position:fixed;left:0;right:0;bottom:0;background:var(--panel);border-top:1px solid var(--line);
 padding:12px 22px;display:flex;gap:12px;align-items:center;box-shadow:0 -2px 12px rgba(0,0,0,.05)}
.bar .grow{flex:1}
.note{font-size:12.5px;color:var(--dim)}
.warn{background:var(--warn-soft);border:1px solid #E8D5AC;border-left:3px solid #C08A2E;
 border-radius:0 8px 8px 0;padding:11px 14px;font-size:13px;color:#4A3E28;margin-bottom:14px}
.empty{color:var(--faint);font-size:13px;padding:22px;text-align:center}
#toast{position:fixed;right:22px;bottom:74px;background:#1A1614;color:#fff;padding:11px 16px;
 border-radius:9px;font-size:13px;opacity:0;transform:translateY(6px);transition:.22s;pointer-events:none}
#toast.on{opacity:1;transform:none}
</style></head><body>
<header><b>FPI — Manuel Giriş</b><span class="sub" id="src"></span>
  <span style="flex:1"></span><span class="sub" id="cnt"></span></header>
<div class="wrap">

  <div class="card">
    <h2>Birim seç</h2>
    <div class="row">
      <div class="f"><label>Taşıyıcı</label><select id="car"></select></div>
      <div class="f"><label>OND</label><select id="ond"></select></div>
      <div class="f"><label>Sezon</label><select id="sea"></select></div>
      <div class="f"><label>Kabin</label><select id="cab"></select></div>
      <div class="f"><label>&nbsp;</label><button id="load" class="primary">Getir</button></div>
    </div>
    <p class="hint" style="margin-top:12px;margin-bottom:0">Listede olmayan bir birim mi
      gireceksiniz? Taşıyıcı/OND kutularına doğrudan yazabilirsiniz.</p>
  </div>

  <div class="card" id="pkgCard" hidden>
    <h2 id="pkgTitle">Paketler</h2>
    <div class="warn" id="inheritNote" hidden></div>
    <table><thead><tr>
      <th style="width:64px">Kademe</th><th>Paket adı</th><th style="width:130px">Fiyat USD</th>
      <th style="width:118px">Durum</th><th style="width:210px">İşlem</th>
    </tr></thead><tbody id="tb"></tbody></table>
    <div style="margin-top:14px"><button id="add" class="ghost">+ Yeni paket ekle</button></div>
  </div>

</div>

<div class="bar">
  <span class="note" id="pend">Bekleyen değişiklik yok</span>
  <span class="grow"></span>
  <button id="clear">Bekleyenleri temizle</button>
  <button id="save" class="primary" disabled>Kaydet</button>
</div>
<div id="toast"></div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const RIGHTS=__RIGHTS__, STATES=["","Included","Paid","Not Included"];
const LBL={"":"— devral —","Included":"Dahil","Paid":"Ücretli","Not Included":"Yok"};
let TREE={}, CUR=null, ROWS=[], PENDING=[];
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("on");
  clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove("on"),2400);}

function fillSel(el,vals,keep){
  const cur=keep?el.value:null;
  el.innerHTML=vals.map(v=>`<option>${esc(v)}</option>`).join("");
  if(cur&&vals.includes(cur))el.value=cur;
}
function onCar(){
  const onds=Object.keys(TREE[$("#car").value]||{}).sort();
  fillSel($("#ond"),onds,true); onOnd();
}
function onOnd(){
  const seas=Object.keys((TREE[$("#car").value]||{})[$("#ond").value]||{});
  fillSel($("#sea"),seas.length?seas:["Summer","Winter"],true); onSea();
}
function onSea(){
  const cabs=((TREE[$("#car").value]||{})[$("#ond").value]||{})[$("#sea").value]||[];
  fillSel($("#cab"),cabs.length?cabs:["Economy","Business"],true);
}

async function loadUnit(){
  const car=$("#car").value.trim().toUpperCase(), ond=$("#ond").value.trim().toUpperCase();
  const sea=$("#sea").value, cab=$("#cab").value;
  if(!car||!/^[A-Z]{3}-[A-Z]{3}$/.test(ond)){toast("OND 'AAA-BBB' biçiminde olmalı");return;}
  const r=await fetch(`/api/unit?car=${car}&ond=${ond}&sea=${sea}&cab=${cab}`);
  const d=await r.json();
  CUR={car,ond,sea,cab};
  ROWS=d.packages.map(p=>({...p,action:"keep",isNew:false,rights:p.rights||{}}));
  $("#pkgTitle").textContent=`${car} · ${ond} · ${sea} · ${cab}`;
  $("#pkgCard").hidden=false;
  const n=$("#inheritNote");
  if(d.packages.length){n.hidden=true;}
  else{n.hidden=false;n.innerHTML="Bu birimde çekilmiş paket yok — sıfırdan gireceksiniz. "+
    "Bir hakkı <b>— devral —</b> bırakırsanız aynı taşıyıcı+marka için başka bir yerde "+
    "girdiğiniz değerden alınır; o da yoksa boş kalır.";}
  render();
}

function render(){
  const tb=$("#tb");
  if(!ROWS.length){tb.innerHTML=`<tr><td colspan="5" class="empty">Paket yok — "Yeni paket ekle" ile başlayın.</td></tr>`;return;}
  tb.innerHTML=ROWS.map((p,i)=>{
    const pill=p.isNew?`<span class="pill p-new">yeni</span>`
             :p.manual?`<span class="pill p-man">manuel</span>`
             :`<span class="pill p-src">çekilmiş</span>`;
    return `<tr class="${p.action==='remove'?'rm':''} ${p.isNew?'new':''}">
      <td><input class="num" style="width:56px;text-align:center" type="number" min="1"
           value="${p.order??i+1}" data-i="${i}" data-k="order"></td>
      <td><input type="text" style="width:100%" value="${esc(p.name)}" data-i="${i}" data-k="name"
           placeholder="paket adı"></td>
      <td><input class="num" type="number" step="0.01" value="${p.price??''}" data-i="${i}"
           data-k="price" placeholder="USD"></td>
      <td>${pill}</td>
      <td>
        <button data-act="rights" data-i="${i}">Haklar</button>
        <button data-act="del" data-i="${i}">${p.action==='remove'?'Geri al':'Sil'}</button>
      </td></tr>
      <tr id="r${i}" hidden><td colspan="5"><div class="rights"><div class="rgrid">
        ${RIGHTS.map(rn=>{const rv=p.rights[rn]||{};
          return `<div class="r"><span title="${esc(rn)}">${esc(rn)}</span>
            <select data-i="${i}" data-r="${esc(rn)}" data-f="state">
              ${STATES.map(s=>`<option value="${s}" ${(rv.state||'')===s?'selected':''}>${LBL[s]}</option>`).join("")}
            </select>
            <input type="text" placeholder="detay (örn. 2×23kg)" value="${esc(rv.detail||'')}"
              data-i="${i}" data-r="${esc(rn)}" data-f="detail"></div>`;}).join("")}
      </div></div></td></tr>`;}).join("");
}

$("#tb").addEventListener("input",e=>{
  const t=e.target,i=+t.dataset.i; if(isNaN(i))return;
  if(t.dataset.r){const rn=t.dataset.r;ROWS[i].rights[rn]=ROWS[i].rights[rn]||{};
    ROWS[i].rights[rn][t.dataset.f]=t.value;}
  else if(t.dataset.k==="price")ROWS[i].price=t.value===""?null:parseFloat(t.value);
  else if(t.dataset.k==="order")ROWS[i].order=parseInt(t.value)||1;
  else ROWS[i][t.dataset.k]=t.value;
  ROWS[i].dirty=true; mark();
});
$("#tb").addEventListener("change",e=>{if(e.target.dataset.r){ROWS[+e.target.dataset.i].dirty=true;mark();}});
$("#tb").addEventListener("click",e=>{
  const b=e.target.closest("button"); if(!b)return; const i=+b.dataset.i;
  if(b.dataset.act==="rights"){const r=$("#r"+i); r.hidden=!r.hidden;}
  else{ROWS[i].action=ROWS[i].action==="remove"?"keep":"remove";ROWS[i].dirty=true;render();mark();}
});
$("#add").addEventListener("click",()=>{
  ROWS.push({order:ROWS.length+1,name:"",price:null,rights:{},action:"keep",isNew:true,dirty:true});
  render(); mark();
});
function mark(){
  const n=ROWS.filter(p=>p.dirty).length;
  $("#save").disabled=!n;
  $("#pend").textContent=n?`${n} paket değişti — kaydedilmedi`:"Bekleyen değişiklik yok";
}

$("#save").addEventListener("click",async()=>{
  const out=[];
  for(const p of ROWS){
    if(!p.dirty)continue;
    if(!p.name.trim()){toast("Paket adı boş olamaz");return;}
    const rights={};
    for(const rn of RIGHTS){const rv=p.rights[rn]||{};
      if(rv.state)rights[rn]=rv.detail?`${rv.state}: ${rv.detail}`:rv.state;}
    out.push({Action:p.action==="remove"?"remove":"upsert",Carrier:CUR.car,
      Origin:CUR.ond.split("-")[0],Destination:CUR.ond.split("-")[1],
      Season:CUR.sea,Cabin:CUR.cab,"Brand Order":p.order,"Brand Name":p.name.trim(),
      "Price USD":p.price==null?"":p.price,...rights});
  }
  const r=await fetch("/api/entries",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(out)});
  const d=await r.json();
  if(!r.ok||d.errors?.length){toast("Hata: "+(d.errors||["bilinmiyor"])[0]);return;}
  ROWS.forEach(p=>p.dirty=false); mark();
  $("#cnt").textContent=`${d.total} bekleyen giriş`;
  toast(`Kaydedildi (${out.length} paket)`);
});
$("#clear").addEventListener("click",async()=>{
  if(!confirm("Bekleyen TÜM manuel girişler silinsin mi? (çekilmiş veri etkilenmez)"))return;
  const r=await fetch("/api/entries",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify([]),});
  await fetch("/api/clear",{method:"POST"});
  $("#cnt").textContent="0 bekleyen giriş"; toast("Temizlendi");
});

["car","ond"].forEach(id=>$("#"+id).addEventListener("change",id==="car"?onCar:onOnd));
$("#sea").addEventListener("change",onSea);
$("#load").addEventListener("click",loadUnit);

(async()=>{
  const d=await(await fetch("/api/index")).json();
  TREE=d.tree; $("#src").textContent=d.dir; $("#cnt").textContent=`${d.entries} bekleyen giriş`;
  fillSel($("#car"),Object.keys(TREE).sort()); onCar();
})();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            page = PAGE.replace("__RIGHTS__", json.dumps(RIGHT_COLS, ensure_ascii=False))
            return self._send(200, page, "text/html; charset=utf-8")
        if u.path == "/api/index":
            return self._send(200, json.dumps(
                {"tree": index(), "dir": DATA["dir"], "entries": len(read_store())},
                ensure_ascii=False))
        if u.path == "/api/unit":
            car = (q.get("car", [""])[0] or "").upper()
            ond = (q.get("ond", [""])[0] or "").upper()
            sea = q.get("sea", [""])[0]
            cab = q.get("cab", [""])[0]
            o, _, d = ond.partition("-")
            pkgs = DATA["units"].get("|".join([car, o, d, sea]), {}).get(cab, [])
            # Pending manual entries win in the editor too, so a second visit
            # shows what you typed rather than the scrape you already corrected.
            pend = {brand_match_key(r["Brand Name"]): r for r in read_store()
                    if (r["Carrier"], r["Origin"], r["Destination"], r["Season"],
                        r["Cabin"]) == (car, o, d, sea, cab)}
            merged = []
            for p in pkgs:
                r = pend.pop(brand_match_key(p["name"]), None)
                merged.append(_from_entry(r, p) if r else p)
            for r in pend.values():
                if r["Action"] != "remove":
                    merged.append(_from_entry(r, None))
            return self._send(200, json.dumps({"packages": merged}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "yok"}))

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else "[]"
        if u.path == "/api/clear":
            write_store([])
            return self._send(200, json.dumps({"total": 0}))
        if u.path == "/api/entries":
            incoming = json.loads(raw)
            rows, errs = validate([{k: ("" if v is None else str(v)) for k, v in r.items()}
                                   for r in incoming])
            if errs:
                return self._send(400, json.dumps({"errors": errs}, ensure_ascii=False))
            cur = read_store()
            idx = {(r["Carrier"], r["Origin"], r["Destination"], r["Season"], r["Cabin"],
                    brand_match_key(r["Brand Name"])): i for i, r in enumerate(cur)}
            for r in rows:
                k = (r["Carrier"], r["Origin"], r["Destination"], r["Season"], r["Cabin"],
                     brand_match_key(r["Brand Name"]))
                if k in idx:
                    cur[idx[k]] = r
                else:
                    idx[k] = len(cur)
                    cur.append(r)
            write_store(cur)
            return self._send(200, json.dumps({"total": len(cur)}))
        return self._send(404, json.dumps({"error": "yok"}))


def _from_entry(r: dict, base: dict | None) -> dict:
    rights = dict((base or {}).get("rights") or {})
    for rn in RIGHT_COLS:
        v = (r.get(rn) or "").strip()
        if v:
            st, _, det = v.partition(":")
            rights[rn] = {"state": st.strip(), "detail": det.strip()}
    return {"order": int(float(r["Brand Order"])) if r.get("Brand Order") else 1,
            "name": r["Brand Name"],
            "price": float(r["Price USD"]) if r.get("Price USD") else None,
            "rights": rights, "manual": True}


def main() -> None:
    out = pick_dir()
    load(out)
    print(f"veri: {out}  ({len(DATA['units'])} birim)")
    print(f"manuel giriş editörü: http://localhost:{PORT}")
    print("kaydedilenler -> manual_data/entries.csv")
    print(f"bitince:  ./.venv/bin/python manual.py apply {out}")
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
