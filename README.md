# Fare Package Intelligence (FPI)

Havayolu **branded-fare** (markalı ücret paketi) verilerini gerçek kaynaklardan
toplayan, normalize eden ve etkileşimli bir dashboard'a döken scraper + analiz
hattı.

**Canlı dashboard:** https://ckrslm97.github.io/fare-package-intelligence/

---

An enterprise-grade pipeline that scrapes **real** airline branded-fare data,
normalizes it into a flat schema, and renders an interactive analytics
dashboard. Turkish notes below; code and comments are in English.

## Ne yapar?

- Excel'deki her `(Taşıyıcı, Origin, Destination)` satırı için kabin bazında
  (Economy / Premium Economy / Business) markalı ücret paketlerini çeker — bir
  rastgele **Yaz** + bir rastgele **Kış** tarihi (dönüş = gidiş + 3 gün, 7 günlük
  fallback penceresi, her taze koşuda yeni rastgele tarihler).
- Kaynak zinciri: **taşıyıcının kendi sitesi → Ubfly** (Enuygun, zayıf paket
  içeriği nedeniyle devre dışı bırakıldı). Kabin başına **en kaliteli merdiven**
  seçilir (en çok paket, en az mantıksız >3× fiyat sıçraması, en ucuz baz);
  birden çok kaynak varsa kaybedenler eksik hakları zenginleştirir.
- Paketler **fiyata göre** sıralanır (sitedeki gibi); her paketin kendi mutlak
  fiyatı hesaplanır (baz + delta çözümü). Kod paylaşımlı (codeshare) uçuşlar
  filtrelenir; kabini adıyla çelişen çöp satırlar elenir; Premium Economy
  ücret aileleri (BA `PREMECON/PESEL`, AC `PL/PF`) ekonomi aramasından geri
  kazanılır.
- 16 hak (El Bagajı, Check-in Bagajı, Koltuk Seçimi, Yemek, Lounge, Priority
  Boarding, Fast Track, Refund, Change, No-Show Refund/Change, Aynı Gün Erken
  Uçuş, WiFi, Extra Baggage, Spor Ekipmanı, Pet) + Mil Kazanımı —
  `Included / Paid / Not Included / Unknown` (ücretli hak → `Paid`, asla
  `Not Included` değil).
- Local/Beyond: uçlardan biri TR ise **Local**, değilse **Beyond**.

## Kurulum

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install playwright openpyxl pytest
playwright install chromium
```

## Çalıştırma

```bash
# Tam koşu (headful tarayıcılar açılır; Cloudflare headful'da geçer)
python -m branded_fare_scraper -i CLAUDE_OND_LIST.xlsx -o output --fresh

# Testler
python -m pytest tests/ -q

# Mevcut ham veriden çıktıları yeniden üret (tarama yok)
python reprocess_raw.py output

# Dashboard + biçimli Excel üret
python to_platform.py <template.html> output dashboard.html
python make_excel.py output output/branded_fares_formatted.xlsx
```

Bayraklar: `--concurrency 6` · `--seasons summer` · `--sources Enuygun,Ubfly` ·
`--seed 42` (tekrarlanabilir tarihler) · `--headless`. Koşu yarıda kesilirse
aynı komut kaldığı yerden devam eder; `--fresh` yeni plan + yeni tarihler.

## Mimari

```
branded_fare_scraper/
  __main__.py      CLI
  runner.py        async orkestrasyon: plan/resume → paralel tarama →
                   kabin-bazlı kalite seçimi + çapraz-kaynak zenginleştirme →
                   doğrulama → yazım
  sources/         kaynak adaptörleri (ubfly, turkish_airlines; enuygun devre dışı);
                   uçuş seçimi PE-farkındalıklı ve merdiven kalite puanlı
  normalization.py marka adı → kanonik alt-tier; fiyat-öncelikli sıralama;
                   kabin tespiti (PE ücret-ailesi kodları dahil); çapraz-kaynak
                   marka eşleme + zenginleştirme; merdiven kalite metrikleri
  pricing.py       "+30" delta → mutlak fiyat çözümü
  amenities.py     hak taksonomisi + Included/Paid/Not Included sınıflandırma
  airports.py      IATA → şehir/ülke kodu (Local/Beyond)
  io_utils.py      girdi okuma + raw/normalized/failed/summary yazımı
  rebuild.py       raw JSONL → model nesneleri (tüm exporterlerin tek kaynağı)
tests/             saf mantık birim testleri (network yok)
output_v*/         koşu çıktıları (raw_data.jsonl, normalized_data.csv/xlsx,
                   report.html, branded_fares_formatted.xlsx, summary.json)
docs/index.html    yayınlanan dashboard (GitHub Pages)
```

Dayanıklılık: ≤10 eşzamanlı görev (tek Chromium, yeniden kullanılan context
havuzu), 429/403/timeout'ta üstel geri çekilmeli 3 deneme, checkpoint/resume
(dondurulmuş tarih planı), kaynak sonuçları (OND, tarih, kabin) bazında
önbelleklenir. THY resmi sitesi PerimeterX CAPTCHA'lıdır; adaptör hızla OTA'ya
düşer (CAPTCHA aşılmaz/aşılmayacak).

## Çıktılar

| Dosya | İçerik |
|---|---|
| `normalized_data.csv` / `.xlsx` | düz satırlar; hak başına bir kolon |
| `raw_data.jsonl` | birim başına ham sonuç (denetim izi) |
| `branded_fares_formatted.xlsx` | renk kodlu, Türkçe başlıklı Excel |
| `report.html` | kart bazlı karşılaştırma raporu |
| `failed_jobs.csv`, `summary.json` | hata/`no availability` listesi + koşu özeti |
| `docs/index.html` | veri gömülü tam dashboard |
