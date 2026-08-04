#!/bin/zsh
# Full re-scrape of the WHOLE reference list (712 international + 66 local),
# split in halves with a health gate between them — same proven shape as
# run_night.sh, but with its own output naming and no dependency on the old
# Downloads template for the final build (the dashboard is rebuilt separately
# from dashboard_template_modern.html so a template change can't fail the run
# after hours of scraping).
#
#   ./run_full.sh <input.xlsx> [concurrency]
set -e
cd /Users/selim/Desktop/FPI_V2

IN=${1:?kullanim: ./run_full.sh <input.xlsx> [concurrency]}
CONC=${2:-8}
STAMP=$(date +%Y%m%d-%H%M%S)
OUT=output_full_$STAMP
mkdir -p $OUT

echo "=== HAZIRLIK $(date +%H:%M:%S) ==="
./.venv/bin/python - "$IN" <<'PY'
import sys, openpyxl
src = sys.argv[1]
ws = openpyxl.load_workbook(src).active
head = [c.value for c in next(ws.iter_rows(max_row=1))]
oi = head.index("OND")
rows = list(ws.iter_rows(min_row=2))
onds = sorted({str(r[oi].value) for r in rows})
half = onds[:len(onds)//2], onds[len(onds)//2:]
for name, keep in zip(("full_A.xlsx", "full_B.xlsx"), half):
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

echo "=== A YARISI $(date +%H:%M:%S)  (eszamanlilik $CONC) ==="
run_half full_A.xlsx ${OUT}_A || true

OK=$(grep -c '| success |' $OUT/run.log || true)
TOT=$(grep -cE '\| (success|partial|no_availability|failed) \|' $OUT/run.log || true)
WALL=$(grep -ciE 'cloudflare|just a moment|forbidden|access denied' $OUT/run.log || true)
NET=$(grep -c 'ERR_INTERNET_DISCONNECTED' $OUT/run.log || true)
echo "A yarisi: $OK/$TOT basarili, duvar sinyali: $WALL, internet kopmasi: $NET"

# Gate: only a collapsed success rate stops the run. A wall mention alone is
# not fatal (the adapter's own cooldown recovers). The internet-drop counter is
# reported separately because 2026-08-04 showed a LOCAL dropout can masquerade
# as a site problem — it is the operator's cue, not an abort condition.
if [ "$TOT" -gt 20 ] && [ $((OK * 100 / TOT)) -lt 25 ]; then
  echo "!!! DURDURULDU — B yarisi baslatilmadi (basari=$OK/$TOT, duvar=$WALL, net=$NET)"
  echo "checkpoint duruyor: ${OUT}_A/state/"
else
  echo "=== B YARISI $(date +%H:%M:%S) ==="
  run_half full_B.xlsx ${OUT}_B || true
fi

echo "=== BIRLESTIRME $(date +%H:%M:%S) ==="
: > $OUT/raw_data.jsonl
for d in ${OUT}_A ${OUT}_B; do
  [ -s $d/raw_data.jsonl ] && cat $d/raw_data.jsonl >> $OUT/raw_data.jsonl
done
echo "ham satir: $(wc -l < $OUT/raw_data.jsonl)"

# One reprocess over the MERGED raw: cross-season reconciliation and ladder
# ordering have to see the whole dataset or the two halves publish differently.
./.venv/bin/python reprocess_raw.py $OUT

echo "=== QA $(date +%H:%M:%S) ==="
./.venv/bin/python qa_check.py $OUT || echo "!!! QA BASARISIZ — yayinlamadan once bak"

echo "=== TAMAM $(date +%H:%M:%S) ==="
wc -l $OUT/normalized_data.csv
echo "cikti: $OUT"
