#!/bin/zsh
# Cekim bitsin -> birlestir + QA + panele gom + push -> raporu yaz -> KAPAT.
#
# Gozetimsiz calisir. Iki sey kasitli:
#
#  1) QA KAPISI yayinla.sh'in icinde. Dusesrse docs/index.html'e DOKUNULMAZ ve
#     push yapilmaz; makine yine de kapanir ama rapor "YAYINLANMADI" der.
#     Yani en kotu ihtimalde yayindaki panel eski haliyle kalir, bozuk veri
#     asla yayina cikmaz.
#  2) Kapatma saati DISKE yazilir. Makine kapandiktan sonra sorulacak tek sey
#     bu; terminal ciktisi ve konusma gecmisi o noktada erisilemez.
#
# Vazgecmek icin (son ana kadar isler):
#     touch /tmp/fpi_no_shutdown
#
set -e
cd /Users/selim/Desktop/FPI_V2
LOG=bitir.log
say(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" | tee -a $LOG; }

say "=== BITIRICI BEKLEMEDE — cekim bitince yayin + kapatma ==="
caffeinate -is -w $$ >/dev/null 2>&1 &

# --- 1. cekim bitsin --------------------------------------------------------
# chain_next.sh'i de bekliyoruz: run_chunk bittiginde zincir hala birkac satir
# calistiriyor olabilir ve arada yeni bir chunk baslatabilir.
#
# KALIP DAR OLMALI. Cinar "branded_fare_scraper" ayni zamanda calisma agacindaki
# bir DIZIN adi, ve `pgrep -f` tum komut satirinda arar: acik bir editor,
# `tail -f branded_fare_scraper/...`, hatta o yolu anan bir yardimci kabuk bile
# esleser ve dongu ASLA bitmezdi (kanitlandi 2026-08-09: `tail -f
# branded_fare_scraper/__init__.py` pgrep'i rc=0 yapiyor). Bu yuzden modul
# yalnizca "python -m branded_fare_scraper" olarak, scriptler de .sh uzantisiyla
# araniyor. Ustune bir tavan: sonsuz bekleyip makineyi acik birakmaktansa
# devam edip raporda soylemek yeglenir.
PAT='(^|/)(run_chunk|chain_next|resume_chunk)\.sh|python -m branded_fare_scraper'
WAITED=0
MAXWAIT=$((10 * 3600))        # 10 saat
while pgrep -f "$PAT" >/dev/null; do
  sleep 30
  WAITED=$((WAITED + 30))
  if [ $WAITED -ge $MAXWAIT ]; then
    say "!!! $((MAXWAIT / 3600)) saattir cekim bitmedi — yine de yayina geciliyor"
    break
  fi
done
sleep 10                      # isleme/QA'nin diske yazmasi bitsin
say "cekim bitti"
for d in output_v3_20260809-*/; do
  say "   ${d%/}: $(wc -l < ${d}raw_data.jsonl 2>/dev/null | tr -d ' ') birim"
done

# --- 2. yayin (QA kapisi iceride) -------------------------------------------
say "=== YAYIN ==="
PUB=EVET
./yayinla.sh >> $LOG 2>&1 || PUB=HAYIR
[ "$PUB" = "EVET" ] && say "yayin TAMAM" || say "!!! yayin DUSTU (QA veya hata) — panel degismedi"

# --- 3. rapor ---------------------------------------------------------------
R=KAPANMA_RAPORU.txt
{
  echo "FPI — LOKAL + IC HAT KOSUSU RAPORU"
  echo "==================================="
  echo
  echo "KAPANMA SAATI : $(date '+%d %B %Y, %A · %H:%M:%S')"
  echo "YAYINLANDI    : $PUB"
  echo
  echo "BU KOSUDA EKLENENLER"
  for d in output_v3_20260809-*/; do
    printf "  %-30s %5s birim   %s\n" "${d%/}" \
      "$(wc -l < ${d}raw_data.jsonl 2>/dev/null | tr -d ' ')" \
      "$(grep -oE 'TAMAM: [0-9]+ satir, [0-9]+ duvar' ${d}stage.log 2>/dev/null | tail -1)"
  done
  echo
  ./.venv/bin/python -c "
import json,glob,os
o=set(); t=0
for d in sorted(glob.glob('output_v3_*')):
    p=os.path.join(d,'raw_data.jsonl')
    if not os.path.exists(p): continue
    for ln in open(p,encoding='utf-8'):
        try: u=json.loads(ln)
        except: continue
        t+=1
        if u.get('origin') and u.get('destination'): o.add((u['origin'],u['destination']))
print(f'TOPLAM (tum partiler): {t} birim · {len(o)} benzersiz OND')
" 2>/dev/null
  # awk gövdesi TEK tırnakta ve $2 KAÇIŞSIZ olmalı: \$2 awk'a sözdizimi hatası
  # verdiriyor ve satır boş çıkıyor (izole testte yakalandı, 2026-08-09).
  echo "DUVAR/CAPTCHA : $(grep -ciE 'cloudflare wall|just a moment|attention required' output_v3_20260809-*/logs/*.log 2>/dev/null | awk -F: '{s+=$2} END{print s+0}')"
  echo
  echo "BU KOSUDA DEGISENLER"
  echo "  · lead bandi 60:120 (LCC'ler 300:330'da satista degil)"
  echo "  · YIC etiketi: iki uc da Turkiye -> yurt ici"
  echo "  · 8 yeni tasiyici kunyesi, 6 yeni havaalani"
  echo "  · lead_days alani: satirlarin karsilastirilabilirligi artik gorunur"
  echo "  · QA: 'ucuyor ama paketi yok' ile 'ucus yok' ayri raporlaniyor"
  echo
  echo "ACIK KONU"
  echo "  Derin bant (300:330) verisinde OND+sezon kovalarinin %52'sinde"
  echo "  tasiyicilar cok farkli satin alma ufkundan fiyatlanmis. Bu kosudaki"
  echo "  yakin bant verisinde ayni oran %2. Eski verinin ne yapilacagi KARAR"
  echo "  BEKLIYOR — panelde lead gostermek mi, yoksa tek bantta yeniden cekmek mi."
  echo
  echo "Panel: https://ckrslm97.github.io/fare-package-intelligence/"
} > $R 2>&1
say "rapor -> $R"

# --- 4. kapat ---------------------------------------------------------------
if [ -f /tmp/fpi_no_shutdown ]; then
  say "kapatma IPTAL (/tmp/fpi_no_shutdown) — makine acik kaliyor"
  exit 0
fi
say "kapatiliyor — $(date '+%H:%M:%S')"
sync
osascript -e 'tell application "System Events" to shut down'
