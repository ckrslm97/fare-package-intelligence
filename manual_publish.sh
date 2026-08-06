#!/bin/zsh
# Panelden (veya Excel'den) girilen manuel kayıtları panele işler.
#
#   ./manual_publish.sh [kaynak_output_dizini]
#
# Kaynağa DOKUNMAZ: birleştirilmiş veri <kaynak>_manual altına yazılır, panel
# ancak QA'nın bloklayan kontrollerini geçerse güncellenir. Yani hatalı bir
# manuel giriş yayındaki veriyi bozamaz.
set -e
cd /Users/selim/Desktop/FPI_V2
PY=./.venv/bin/python

SRC=${1:-$(ls -dt output_v2_*/ 2>/dev/null | grep -v '_A/\|_B/\|_manual/' | head -1 | sed 's:/$::')}
[ -n "$SRC" ] && [ -f "$SRC/raw_data.jsonl" ] || {
  echo "kaynak bulunamadı. kullanım: ./manual_publish.sh <output_dizini>"; exit 1; }
DST="${SRC}_manual"

log(){ echo "[$(date +%H:%M:%S)] $*"; }

log "kaynak: $SRC"
log "=== 1/4 manuel kayıtları birleştir ==="
$PY manual.py apply "$SRC" --out "$DST"

log "=== 2/4 yeniden işle ==="
$PY reprocess_raw.py "$DST"

log "=== 3/4 QA ==="
if ! $PY qa_check.py "$DST"; then
  log "!!! QA BAŞARISIZ — panele DOKUNULMADI. Veri $DST içinde duruyor."
  exit 2
fi

log "=== 4/4 panele göm ==="
$PY to_platform.py dashboard_template_modern.html "$DST" docs/index.html

log "TAMAM — docs/index.html güncellendi"
echo
echo "yayınlamak için:"
echo "  git add docs/index.html && git commit -m 'Manuel girişleri işle' && git push"
