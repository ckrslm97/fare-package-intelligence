#!/bin/zsh
# Resume the interrupted v2/v3 scrape and finish it.
#
#   ./run_resume.sh <stamp> [concurrency]      e.g. ./run_resume.sh 20260805-034945
#
# Half A is RESUMED: no --fresh, so the frozen plan and the checkpoint both
# survive and the units already collected are neither re-fetched nor re-dated.
# Half B has never run, so it starts fresh. Publishing stays gated on QA — the
# dashboard currently online is not touched unless the finished dataset earns it.
set -e
cd /Users/selim/Desktop/FPI_V2

STAMP=${1:?kullanim: ./run_resume.sh <stamp> [concurrency]}
CONC=${2:-8}
OUT=output_v2_$STAMP
mkdir -p $OUT

log(){ echo "[$(date +%H:%M:%S)] $*"; }

common=(--sources Enuygun --merge-cross-date --channel chrome
        --concurrency $CONC --source-concurrency $CONC
        --seasons summer,winter --date-mode far3 --far-lead 300:330
        --log-level INFO)

if [ -f ${OUT}_A/state/checkpoint.json ]; then
  DONE=$(./.venv/bin/python -c "import json;print(len(json.load(open('${OUT}_A/state/checkpoint.json'))['completed']))")
  log "=== A YARISI DEVAM (checkpoint: $DONE birim hazir, --fresh YOK) ==="
else
  log "=== A YARISI (checkpoint yok, bastan) ==="
fi
./.venv/bin/python -m branded_fare_scraper -i v2_A.xlsx -o ${OUT}_A "${common[@]}" \
  >> $OUT/run.log 2>&1 || true

health(){
  local ok tot wall net
  ok=$(grep -c '| success |' $OUT/run.log || true)
  tot=$(grep -cE '\| (success|partial|no_availability|failed) \|' $OUT/run.log || true)
  wall=$(grep -ciE 'cloudflare|just a moment|forbidden|access denied' $OUT/run.log || true)
  net=$(grep -c 'browser has been closed' $OUT/run.log || true)
  echo "$ok $tot $wall $net"
}
read OK TOT WALL CRASH <<< "$(health)"
log "A yarisi: $OK/$TOT basarili, duvar: $WALL, tarayici-kopmasi: $CRASH"

if [ "$TOT" -gt 20 ] && [ $((OK * 100 / TOT)) -lt 25 ]; then
  log "!!! DURDURULDU — B baslatilmadi (basari=$OK/$TOT). Yayindaki veriye DOKUNULMADI."
  exit 1
fi

if [ -f ${OUT}_B/state/checkpoint.json ]; then
  log "=== B YARISI DEVAM (checkpoint bulundu) ==="
  ./.venv/bin/python -m branded_fare_scraper -i v2_B.xlsx -o ${OUT}_B "${common[@]}" \
    >> $OUT/run.log 2>&1 || true
else
  log "=== B YARISI (ilk kez) ==="
  ./.venv/bin/python -m branded_fare_scraper -i v2_B.xlsx -o ${OUT}_B --fresh "${common[@]}" \
    >> $OUT/run.log 2>&1 || true
fi
read OK TOT WALL CRASH <<< "$(health)"
log "toplam: $OK/$TOT basarili, duvar: $WALL, tarayici-kopmasi: $CRASH"
log "tarayici yeniden baslatma: $(grep -c 'Browser is gone' $OUT/run.log || true)"

log "=== BIRLESTIRME ==="
: > $OUT/raw_data.jsonl
for d in ${OUT}_A ${OUT}_B; do
  [ -s $d/raw_data.jsonl ] && cat $d/raw_data.jsonl >> $OUT/raw_data.jsonl
done
log "ham satir: $(wc -l < $OUT/raw_data.jsonl)"

./.venv/bin/python reprocess_raw.py $OUT

log "=== QA ==="
if ./.venv/bin/python qa_check.py $OUT > $OUT/qa.txt 2>&1; then
  cat $OUT/qa.txt
  log "=== ARAYUZE GOMME ==="
  ./.venv/bin/python to_platform.py dashboard_template_modern.html $OUT docs/index.html
  log "gomuldu: docs/index.html"
else
  cat $OUT/qa.txt
  log "!!! QA BASARISIZ — yayindaki veriye DOKUNULMADI, yeni veri $OUT icinde"
  exit 2
fi

log "=== TAMAM ==="
wc -l $OUT/normalized_data.csv
