"""Canlı ilerleme paneli — uzun koşular sırasında http://localhost:8765

Ne yapar: en son değişen ``output_*/state/`` klasörünü bulur, plan.json'daki
toplam birim sayısını ve checkpoint.json'daki tamamlanan birim sayısını okur,
tamamlanma hızından tahmini kalan süreyi hesaplar ve 2 saniyede bir kendini
yenileyen bir sayfada gösterir. Koşuya müdahale etmez, sadece okur.

Kullanım:
    python live_progress.py            # en güncel output_* klasörünü izler
    python live_progress.py output_v12 # belirli bir klasörü izler
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8765
FORCED = Path(sys.argv[1]) if len(sys.argv) > 1 else None

# (timestamp, done) örnekleri — hız ve ETA için kayan pencere.
_samples: list[tuple[float, int]] = []


def _out_dir() -> Path | None:
    if FORCED:
        p = FORCED if FORCED.is_absolute() else ROOT / FORCED
        return p if (p / "state" / "plan.json").exists() else None
    cands = list(ROOT.glob("output_*/state/checkpoint.json"))
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime).parent.parent


def _last_log_line(out: Path) -> str:
    logs = sorted(out.glob("logs/*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return ""
    try:
        with logs[-1].open("rb") as f:
            f.seek(max(0, f.seek(0, 2) - 4096))
            lines = f.read().decode("utf-8", "replace").strip().splitlines()
        return lines[-1] if lines else ""
    except OSError:
        return ""


def snapshot() -> dict:
    out = _out_dir()
    if not out:
        return {"error": "output_* altında state/plan.json bulunamadı"}
    try:
        plan = json.loads((out / "state" / "plan.json").read_text("utf-8"))
        total = len(plan.get("units", []))
    except (OSError, json.JSONDecodeError):
        total = 0
    try:
        ck = json.loads((out / "state" / "checkpoint.json").read_text("utf-8"))
        done = len(ck.get("completed", []))
    except (OSError, json.JSONDecodeError):
        done = 0

    now = time.time()
    _samples.append((now, done))
    while _samples and now - _samples[0][0] > 600:      # 10 dk pencere
        _samples.pop(0)
    rate = eta_s = None
    if len(_samples) >= 2:
        dt = _samples[-1][0] - _samples[0][0]
        dd = _samples[-1][1] - _samples[0][1]
        if dt > 30 and dd > 0:
            rate = dd / dt * 60                          # birim/dk
            if total > done:
                eta_s = (total - done) / (dd / dt)

    ck_path = out / "state" / "checkpoint.json"
    stale_s = now - ck_path.stat().st_mtime if ck_path.exists() else None
    return {
        "dir": out.name, "total": total, "done": done,
        "pct": round(100 * done / total, 1) if total else 0,
        "rate_per_min": round(rate, 1) if rate else None,
        "eta_min": round(eta_s / 60) if eta_s else None,
        "idle_s": round(stale_s) if stale_s is not None else None,
        "last_log": _last_log_line(out)[-220:],
        "ts": time.strftime("%H:%M:%S"),
    }


_PAGE = """<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">
<title>FPI canlı ilerleme</title>
<style>
:root{color-scheme:light dark;
  --bg:#fff;--card:#f6f6f4;--ink:#111;--dim:#666;--bar:#1D9E75;--track:#e5e5e0}
@media (prefers-color-scheme:dark){
  :root{--bg:#191919;--card:#242424;--ink:#eee;--dim:#999;--track:#333}}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,system-ui,sans-serif;padding:2rem;max-width:640px}
h1{font-size:17px;font-weight:600;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px}
.big{font-size:34px;font-weight:700;font-variant-numeric:tabular-nums}
.track{height:12px;background:var(--track);border-radius:6px;overflow:hidden;margin:10px 0 18px}
.fill{height:100%;background:var(--bar);border-radius:6px;transition:width .6s}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.kpi{background:var(--card);border-radius:8px;padding:10px 14px}
.kpi .l{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.kpi .v{font-size:19px;font-weight:600;font-variant-numeric:tabular-nums}
.log{margin-top:18px;background:var(--card);border-radius:8px;padding:10px 14px;
  font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);
  white-space:nowrap;overflow-x:auto}
.done .fill{background:#0F6E56}
</style></head><body>
<h1>FPI canlı ilerleme</h1>
<div class="sub" id="sub">bağlanıyor…</div>
<span class="big" id="pct">—</span>
<div class="track"><div class="fill" id="fill" style="width:0%"></div></div>
<div class="grid">
 <div class="kpi"><div class="l">Birim</div><div class="v" id="units">—</div></div>
 <div class="kpi"><div class="l">Hız</div><div class="v" id="rate">—</div></div>
 <div class="kpi"><div class="l">Tahmini kalan</div><div class="v" id="eta">—</div></div>
 <div class="kpi"><div class="l">Son güncelleme</div><div class="v" id="ts">—</div></div>
</div>
<div class="log" id="log"></div>
<script>
async function tick(){
  try{
    const d = await (await fetch('/data')).json();
    if(d.error){ document.getElementById('sub').textContent = d.error; return; }
    const fin = d.total && d.done >= d.total;
    document.body.classList.toggle('done', fin);
    document.getElementById('sub').textContent =
      d.dir + (fin ? ' — koşu tamamlandı' :
        (d.idle_s != null && d.idle_s > 300 ? ' — aktif koşu yok (son durum)' : ' — çalışıyor'));
    document.getElementById('pct').textContent = '%' + d.pct;
    document.getElementById('fill').style.width = d.pct + '%';
    document.getElementById('units').textContent = d.done + ' / ' + d.total;
    document.getElementById('rate').textContent = d.rate_per_min ? d.rate_per_min + '/dk' : '—';
    document.getElementById('eta').textContent =
      fin ? 'bitti' : (d.eta_min != null ? '~' + d.eta_min + ' dk' : '—');
    document.getElementById('ts').textContent = d.ts;
    document.getElementById('log').textContent = d.last_log || '';
  }catch(e){ document.getElementById('sub').textContent = 'sunucuya ulaşılamıyor'; }
}
tick(); setInterval(tick, 2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/data"):
            body = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        else:
            body = _PAGE.encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                           # sessiz sunucu
        pass


if __name__ == "__main__":
    print(f"canlı ilerleme paneli: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
