#!/bin/zsh
# One chunk of the collection, at the full reference setting.
#
#   ./run_chunk.sh <input.xlsx> [concurrency] [check-window-days]
#
# Writes to its OWN stamped directory: an earlier chunk's data is never touched
# and never re-scraped. The chunks are merged at the end (raw_data.jsonl files
# concatenate; iter_raw_records dedupes on unit key and keeps the stronger
# record), so splitting the work across nights costs nothing.
#
# Publishing is NOT part of this script. docs/ is never touched.
#
# SHUTDOWN is OPT-IN and off by default. Authorisation to power the machine off
# was given for one specific run and does not silently carry over to the next
# one, so this asks for it explicitly — create the file below any time before
# the run finishes and it will shut down (after a 3-minute dialog with a cancel
# button); leave it absent and the machine stays on:
#
#     touch /tmp/fpi_shutdown
#
set -e
cd /Users/selim/Desktop/FPI_V2

IN=${1:?kullanim: ./run_chunk.sh <input.xlsx> [concurrency] [check-window-days]}
CONC=${2:-8}
CHECKDAYS=${3:-7}
# Nezaket araligi. Olculdu (2026-08-08): kapi gecen surenin %101'ini yiyor, yani
# butun isciler kapida bosta bekliyor ve --concurrency artirmak SIFIR kazanc
# verir. Tek gercek kaldirac bu deger. Varsayilan 3.0 ile 6 partide 0 duvar
# alindi; asagidaki dosya varsa onun degeri kullanilir, boylece hizlandirma
# kalici degil ve dosya silinince eski ayara donuluyor.
#
#     echo 2.0 > .enuygun_interval     # hizlandir
#     rm -f  .enuygun_interval         # kanitlanmis 3.0'a don
#
INTERVAL=3.0
[ -s .enuygun_interval ] && INTERVAL=$(tr -d ' \n' < .enuygun_interval)
# Kalkis-oncesi gun bandi. Varsayilan 300:330 "derin band" fiyatlamasi icin,
# ANCAK dusuk maliyetli tasiyicilar o kadar ileriye bilet satmiyor. Olculdu
# (2026-08-09, canli): 300:330 bandinda TK ve XQ 2027-06-05'i verirken PC ayni
# "Yaz" birimi icin 2026-09-21'e (43 gun) dustu -- ayni etiket, 257 gun farkli
# satin alma ufku, dolayisiyla karsilastirilamaz fiyat. LCC agirlikli listeler
# icin bandi herkesin satista oldugu yere cekmek gerekiyor.
#
#     echo 60:120 > .far_lead     # LCC listeleri
#     rm -f .far_lead             # referans derin band (300:330)
#
LEAD=300:330
[ -s .far_lead ] && LEAD=$(tr -d ' \n' < .far_lead)
WANT=/tmp/fpi_shutdown          # varsa kapat
CANCEL=/tmp/fpi_no_shutdown     # varsa kapatma (sayac sirasinda da isler)
rm -f "$CANCEL"

STAMP=$(date +%Y%m%d-%H%M%S)
OUT=output_v3_$STAMP
mkdir -p $OUT
log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a $OUT/stage.log; }

# Preflight FIRST: a missing carrier name or airport entry does not error, it
# quietly deletes rows. Cheaper to find in a second here than in a report later.
if ! ./.venv/bin/python preflight.py "$IN" 2>&1 | tee -a $OUT/stage.log | tail -5; then
  log "!!! ON KONTROL BASARISIZ — kosu baslatilmadi"; exit 1
fi

log "=== $IN · 7/$CHECKDAYS/$CHECKDAYS · esz=$CONC · aralik=${INTERVAL}sn · lead=$LEAD -> $OUT ==="
./.venv/bin/python -m branded_fare_scraper \
  -i "$IN" -o "$OUT" --fresh --sources Enuygun --merge-cross-date \
  --channel chrome --concurrency $CONC --source-concurrency $CONC \
  --seasons summer,winter --date-mode far3 --far-lead $LEAD \
  --check-window-days $CHECKDAYS --enuygun-interval $INTERVAL \
  --log-level INFO >> $OUT/run.log 2>&1 || log "!!! kosu hata verdi (checkpoint duruyor)"

log "=== ISLEME ==="
./.venv/bin/python reprocess_raw.py "$OUT" 2>&1 | tee -a $OUT/stage.log || true
log "=== QA ==="
./.venv/bin/python qa_check.py "$OUT" > $OUT/qa.txt 2>&1 || true
tail -20 $OUT/qa.txt | tee -a $OUT/stage.log

ROWS=$(($(wc -l < $OUT/normalized_data.csv 2>/dev/null || echo 1) - 1))
WALL=$(grep -ciE 'cloudflare|just a moment|attention required' $OUT/run.log 2>/dev/null | tr -d ' \n')
log "=== TAMAM: $ROWS satir, ${WALL:-0} duvar — panele GOMULMEDI (kasitli) ==="
chmod -w $OUT/raw_data.jsonl $OUT/normalized_data.csv $OUT/evidence.csv 2>/dev/null || true

if [ ! -f "$WANT" ]; then
  log "kapatma istenmedi ($WANT yok) — makine acik kaliyor"
  exit 0
fi
log "kapatma sayaci basladi (180s) — iptal: dialogdaki KAPATMA veya touch $CANCEL"
ANSWER=$(osascript <<OSA 2>/dev/null || echo "gave up:true"
tell application "System Events"
    activate
    set r to display dialog "FPI chunk bitti.
$ROWS satir, ${WALL:-0} duvar.
Veri: $OUT  (panele GOMULMEDI)

Bilgisayar 3 dakika icinde kapanacak." with title "FPI — kapatma" buttons {"KAPATMA"} default button 1 giving up after 180 with icon caution
    if gave up of r then
        return "gave up:true"
    else
        return "cancelled"
    end if
end tell
OSA
)
if [ -f "$CANCEL" ] || [ "$ANSWER" = "cancelled" ]; then
  log "kapatma IPTAL edildi"; exit 0
fi
log "kapatiliyor..."
osascript -e 'tell application "System Events" to shut down'
