#!/bin/zsh
# Full re-scrape with the 2026-08-04 accuracy fixes (cross-flight right merge,
# 5-date blocks, right-coverage QA). Writes to a NEW output directory and
# NEVER touches the published dashboard unless QA passes — the currently
# published dataset stays exactly as it is until this run has earned the swap.
#
#   ./run_v2.sh <input.xlsx> [concurrency]
set -e
cd /Users/selim/Desktop/FPI_V2

IN=${1:?kullanim: ./run_v2.sh <input.xlsx> [concurrency]}
CONC=${2:-8}
STAMP=$(date +%Y%m%d-%H%M%S)
OUT=output_v2_$STAMP
mkdir -p $OUT

log(){ echo "[$(date +%H:%M:%S)] $*"; }

log "=== HAZIRLIK ==="
./.venv/bin/python - "$IN" <<'PY'
import sys, openpyxl
src = sys.argv[1]
ws = openpyxl.load_workbook(src).active
head = [c.value for c in next(ws.iter_rows(max_row=1))]
oi = head.index("OND")
rows = list(ws.iter_rows(min_row=2))
onds = sorted({str(r[oi].value) for r in rows})
half = onds[:len(onds)//2], onds[len(onds)//2:]
for name, keep in zip(("v2_A.xlsx", "v2_B.xlsx"), half):
    wb = openpyxl.Workbook(); w = wb.active; w.append(head)
    n = 0
    for r in rows:
        if str(r[oi].value) in keep:
            w.append([c.value for c in r]); n += 1
    wb.save(name)
    print(f"  {name}: {len(keep)} OND, {n} cift")
PY

run_half () {                       # $1=input  $2=output
  ./.venv/bin/python -m branded_fare_scraper \
    -i "$1" -o "$2" --fresh --sources Enuygun --merge-cross-date \
    --channel chrome --concurrency $CONC --source-concurrency $CONC \
    --seasons summer,winter --date-mode far3 --far-lead 300:330 \
    --log-level INFO >> $OUT/run.log 2>&1
}

health () {                         # yarim bittikten sonra saglik ozeti
  local ok tot wall net
  ok=$(grep -c '| success |' $OUT/run.log || true)
  tot=$(grep -cE '\| (success|partial|no_availability|failed) \|' $OUT/run.log || true)
  wall=$(grep -ciE 'cloudflare|just a moment|forbidden|access denied' $OUT/run.log || true)
  net=$(grep -c 'ERR_INTERNET_DISCONNECTED' $OUT/run.log || true)
  echo "$ok $tot $wall $net"
}

log "=== A YARISI (eszamanlilik $CONC) ==="
run_half v2_A.xlsx ${OUT}_A || true
read OK TOT WALL NET <<< "$(health)"
log "A yarisi: $OK/$TOT basarili, duvar: $WALL, internet kopmasi: $NET"

# Gate: only a collapsed success rate stops the run. A wall alone is not fatal
# (the adapter's own cooldown recovers); the network counter is reported
# separately because a LOCAL dropout can masquerade as a site problem.
if [ "$TOT" -gt 20 ] && [ $((OK * 100 / TOT)) -lt 25 ]; then
  log "!!! DURDURULDU — B yarisi baslatilmadi (basari=$OK/$TOT)"
  log "checkpoint duruyor: ${OUT}_A/state/  — yayindaki veriye DOKUNULMADI"
  exit 1
fi

log "=== B YARISI ==="
run_half v2_B.xlsx ${OUT}_B || true
read OK TOT WALL NET <<< "$(health)"
log "toplam: $OK/$TOT basarili, duvar: $WALL, internet kopmasi: $NET"

log "=== BIRLESTIRME ==="
: > $OUT/raw_data.jsonl
for d in ${OUT}_A ${OUT}_B; do
  [ -s $d/raw_data.jsonl ] && cat $d/raw_data.jsonl >> $OUT/raw_data.jsonl
done
log "ham satir: $(wc -l < $OUT/raw_data.jsonl)"

# One reprocess over the MERGED raw: cross-season reconciliation and ladder
# ordering have to see the whole dataset or the two halves publish differently.
./.venv/bin/python reprocess_raw.py $OUT

log "=== QA ==="
if ./.venv/bin/python qa_check.py $OUT > $OUT/qa.txt 2>&1; then
  cat $OUT/qa.txt
  log "=== ARAYUZE GOMME ==="
  ./.venv/bin/python to_platform.py dashboard_template_modern.html $OUT docs/index.html
  log "gomuldu: docs/index.html"
else
  cat $OUT/qa.txt
  log "!!! QA BASARISIZ — yayindaki veriye DOKUNULMADI, yeni veri $OUT icinde duruyor"
  exit 2
fi

log "=== TAMAM ==="
wc -l $OUT/normalized_data.csv
