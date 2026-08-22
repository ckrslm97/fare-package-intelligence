#!/bin/zsh
# Toplanan bütün partileri birleştir, işle, QA'dan geçir ve panele YAYINLA.
#
#   ./yayinla.sh [ek_dizin ...]
#
# Claude gerekmez. Tek komut, baştan sona.
#
#   output_v3_*/  ->  birleştir  ->  işle  ->  QA  ->  panele göm  ->  git push
#
# QA KAPISI: bloklayan kontrollerden biri bile düşerse docs/index.html'e
# DOKUNULMAZ ve push yapılmaz. Yani bozuk bir veri seti yayına asla çıkamaz;
# en kötü ihtimalle yayındaki panel eskisi gibi kalır.
#
# Birleştirme güvenli: raw_data.jsonl dosyaları uç uca eklenir ve
# iter_raw_records her birimi (taşıyıcı, kalkış, varış, sezon) anahtarıyla
# tekilleştirir — aynı birim iki partide de varsa panele tek satır gider.
set -e
cd /Users/selim/Desktop/FPI_V2
PY=./.venv/bin/python

STAMP=$(date +%Y%m%d-%H%M%S)
MERGED=merged_$STAMP
LOG=yayin.log
say(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" | tee -a $LOG; }

# --- 1. hangi partiler ------------------------------------------------------
DIRS=()
for d in output_v3_*/; do
  [ -s "${d}raw_data.jsonl" ] && DIRS+=("${d%/}")
done
for extra in "$@"; do
  [ -s "$extra/raw_data.jsonl" ] && DIRS+=("$extra") || say "!! atlandi (raw yok): $extra"
done
[ ${#DIRS[@]} -gt 0 ] || { say "!!! birlestirilecek parti yok"; exit 1; }

say "=== YAYIN BASLIYOR · ${#DIRS[@]} parti ==="
for d in $DIRS; do say "   $d ($(wc -l < $d/raw_data.jsonl | tr -d ' ') birim)"; done

# --- 2. birlestir -----------------------------------------------------------
mkdir -p $MERGED
: > $MERGED/raw_data.jsonl
for d in $DIRS; do cat $d/raw_data.jsonl >> $MERGED/raw_data.jsonl; done
# summary.json Arsiv sekmesi icin gerekli; en yenisini al.
# -f SART: tamamlanan partilerin dosyalari salt-okunur (chmod -w) yapiliyor,
# duz cp hedefi de 444 olarak yaratir ve SONRAKI cp "Permission denied" ile
# duser -- set -e yuzunden butun yayin orada olur. -f hedefi silip yeniden
# yazar. (2026-08-09'da 8 partilik ilk birlestirmede yakalandi.)
for d in $DIRS; do [ -f "$d/summary.json" ] && cp -f "$d/summary.json" $MERGED/ ; done
chmod u+w $MERGED/summary.json 2>/dev/null || true
say "birlesik ham: $(wc -l < $MERGED/raw_data.jsonl | tr -d ' ') satir -> $MERGED"

# --- 3. isle ----------------------------------------------------------------
say "=== ISLEME ==="
$PY reprocess_raw.py $MERGED 2>&1 | tee -a $LOG
if [ "${pipestatus[1]}" -ne 0 ]; then
  say "!!! ISLEME BASARISIZ — panele DOKUNULMADI"
  exit 4
fi

# --- 4. QA kapisi -----------------------------------------------------------
say "=== QA ==="
if ! $PY qa_check.py $MERGED > $MERGED/qa.txt 2>&1; then
  cat $MERGED/qa.txt | tee -a $LOG
  say "!!! QA BASARISIZ — panele DOKUNULMADI, push YAPILMADI"
  say "    veri $MERGED icinde duruyor, yayindaki panel degismedi"
  exit 2
fi
tail -20 $MERGED/qa.txt | tee -a $LOG

# --- 5. panele goem ---------------------------------------------------------
# `cmd | tee` ile `set -e` ISE YARAMAZ: zsh'de pipefail kapali oldugu icin
# borunun cikis kodu tee'nin 0'idir. to_platform.py cikis 1 verse bile (orn.
# sablondaki EMBEDDED isaretcisi bozulmus) docs/index.html'e HIC dokunulmaz,
# ardindan `git diff --quiet` basarili olur, script "panel degismedi" deyip
# 0 ile biter ve cagiran "YAYINLANDI: EVET" yazar. Gozetimsiz kosuda bu,
# eski panelin uzerine hicbir sey yazilmadan basarili rapor demek olurdu.
# ${pipestatus[1]} borunun ILK komutunun kodudur (zsh, 1-indexli).
say "=== PANELE GOMME ==="
$PY to_platform.py dashboard_template_modern.html $MERGED docs/index.html 2>&1 | tee -a $LOG
if [ "${pipestatus[1]}" -ne 0 ]; then
  say "!!! PANELE GOMME BASARISIZ — docs/index.html'e DOKUNULMADI, push YAPILMADI"
  exit 3
fi

# --- 6. yayinla -------------------------------------------------------------
ROWS=$(($(wc -l < $MERGED/normalized_data.csv) - 1))
ONDS=$($PY -c "
import csv;r=list(csv.DictReader(open('$MERGED/normalized_data.csv',encoding='utf-8')))
print(len({(x['Origin'],x['Destination']) for x in r}))")
if git diff --quiet docs/index.html; then
  say "panel degismedi — push gerekmiyor"
else
  git add docs/index.html
  git commit -q -m "Veri güncellemesi: $ROWS satır · $ONDS OND ($(date +%Y-%m-%d))"
  git push -q origin main
  say "PUSH EDILDI: $ROWS satir · $ONDS OND"
fi

say "=== TAMAM ==="
say "panel: https://ckrslm97.github.io/fare-package-intelligence/"
