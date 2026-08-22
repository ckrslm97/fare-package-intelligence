#!/usr/bin/env python3
"""Yayın panelinden DÜZENLEME sürümü üretir.

    ./.venv/bin/python make_editor.py [cikti.html]

Neden ayrı bir şablon değil de bayrak: iki HTML dosyası zamanla birbirinden
ayrışır — birine eklenen bir grafik ötekine eklenmez, biri düzeltilir öteki
unutulur. Bu projede aynı tuzağa daha önce düşüldü; ``publish.py`` tam bunun
için var (runner ile reprocess'in satırları ayrışmasın diye). Burada da tek
kaynak ``dashboard_template_modern.html``; bu script yalnız iki şeyi değiştirir:

  1. ``EDITOR_MODE``  false -> true
  2. ``PANEL_SHELL_B64``  boş -> panelin VERİSİZ kopyası (base64)

İkincisi dışa aktarma için: düzenleme sayfası "güncel paneli indir" dediğinde
kendi DOM'unu kopyalayamaz (render sırasında değişmiştir), bu yüzden temiz bir
kabuk taşır ve veriyi onun içine yazar. Kabuk verisiz olduğu için ~300 KB;
dosyayı iki katına çıkarmaz.

ÇIKTI ``docs/`` İÇİNE YAZILMAZ. Orası GitHub Pages ile yayınlanıyor; düzenleme
sürümü herkese açık olmamalı. Varsayılan hedef masaüstü.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "dashboard_template_modern.html"
PANEL = ROOT / "docs" / "index.html"

S_MARK = "/*__EMBEDDED_DATA_START__*/"
E_MARK = "/*__EMBEDDED_DATA_END__*/"


def _between(text: str, start: str, end: str) -> tuple[int, int]:
    i = text.find(start)
    j = text.find(end)
    if i < 0 or j < 0 or j < i:
        raise SystemExit(f"işaretçi bulunamadı: {start} … {end}")
    return i + len(start), j


def build() -> str:
    if not PANEL.exists():
        raise SystemExit(f"yayın paneli yok: {PANEL} — önce ./yayinla.sh")
    template = TEMPLATE.read_text(encoding="utf-8")
    panel = PANEL.read_text(encoding="utf-8")

    # Veriyi yayındaki panelden al: şablonun içindeki örnek veri değil, GERÇEK
    # yayınlanmış veri düzenlenmeli.
    di, dj = _between(panel, S_MARK, E_MARK)
    data_json = panel[di:dj].strip()
    try:
        count = len(json.loads(data_json).get("fares", []))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"yayındaki veri okunamadı: {exc}")

    # 1) Dışa aktarma kabuğu: ŞABLON + boş veri. Şablonu kullanmak önemli —
    #    kabuğa yayındaki 20 MB'lık veriyi koymak dosyayı iki katına çıkarırdı.
    shell = template[: template.find(S_MARK) + len(S_MARK)] + "{}" + template[template.find(E_MARK):]
    shell_b64 = base64.b64encode(shell.encode("utf-8")).decode("ascii")

    # 2) Düzenleme sürümü = şablon + gerçek veri + bayrak + kabuk
    out = template[: template.find(S_MARK) + len(S_MARK)] + data_json + template[template.find(E_MARK):]

    out, n1 = re.subn(
        r"/\*__EDITOR_MODE__\*/false/\*__EDITOR_MODE_END__\*/",
        "/*__EDITOR_MODE__*/true/*__EDITOR_MODE_END__*/", out)
    out, n2 = re.subn(
        r'/\*__SHELL_START__\*/""/\*__SHELL_END__\*/',
        f'/*__SHELL_START__*/"{shell_b64}"/*__SHELL_END__*/', out)
    if n1 != 1 or n2 != 1:
        raise SystemExit(f"yer tutucular beklendiği gibi değil (mode={n1}, shell={n2})")

    # Başlıktan ayırt edilebilsin: iki dosya bir arada durunca hangisinin
    # düzenleme sürümü olduğu sekme adından anlaşılmalı.
    out = out.replace("<title>", "<title>[DÜZENLEME] ", 1)
    print(f"veri: {count} tarife · kabuk: {len(shell)//1024} KB")
    return out


def main() -> None:
    dest = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else (
        Path.home() / "Desktop" / "FPI_DUZENLEME" /
        f"fpi_duzenleme_{datetime.now():%Y-%m-%d}.html")
    if "docs" in dest.resolve().parts:
        raise SystemExit("HAYIR: docs/ yayınlanan dizin — düzenleme sürümü oraya yazılamaz.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    html = build()
    dest.write_text(html, encoding="utf-8")
    print(f"düzenleme sürümü -> {dest}  ({dest.stat().st_size//1024//1024} MB)")


if __name__ == "__main__":
    main()
