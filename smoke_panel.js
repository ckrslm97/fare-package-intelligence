/* Paneli GERÇEKTEN açan duman testi.
 *
 *   node smoke_panel.js <panel.html> [--editor]
 *
 * Neden gerek var: `node --check` yalnızca SÖZDİZİMİNE bakar. 2026-08-23'te
 * var olmayan bir yardımcı (money()) çağrıldı; sözdizimi kusursuzdu, sayfa
 * açıldığında ReferenceError atıp bütün kart alanını boş bıraktı ve hata
 * yalnızca tarayıcı konsolunda kaldı. Ekranda görünen tek şey boşluktu.
 *
 * Bu test sayfayı jsdom içinde açar, konsol hatalarını toplar ve kartların
 * gerçekten çizildiğini doğrular — yani "açılıyor mu" sorusunu sorar.
 */
const fs = require("fs");
const path = require("path");

const file = process.argv[2];
const wantEditor = process.argv.includes("--editor");
if (!file) { console.error("kullanim: node smoke_panel.js <panel.html> [--editor]"); process.exit(2); }

const { JSDOM } = require(path.join(
  "/private/tmp/claude-501/-Users-selim-Desktop-FPI-SCRAPING",
  "1de1b9e8-3012-44fb-bb56-7bdd2831fda7/scratchpad/node_modules/jsdom"));

const errors = [];
/* Sayfa açılışında atılan hata jsdom'un sanal konsolunu aşıp süreci
   çökertebiliyor (money() vakasında olan buydu: yığın izi basılıp node
   ölüyordu, test raporu hiç yazılmıyordu). Süreç düzeyinde yakalayıp DÜZGÜN
   bir başarısızlık raporu üretmek gerekiyor — çöken test, yakalayan testten
   ayırt edilemez. */
function bail(kind, err) {
  console.log(`  !!! ${kind}: ${(err && err.message) || err}`);
  console.log("  !!! sayfa açılışta çöktü — kartlar çizilemez");
  process.exit(1);
}
process.on("uncaughtException", e => bail("çalışma zamanı hatası", e));
process.on("unhandledRejection", e => bail("yakalanmamış reddetme", e));
const dom = new JSDOM(fs.readFileSync(file, "utf8"), {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: new (require(path.join(
    "/private/tmp/claude-501/-Users-selim-Desktop-FPI-SCRAPING",
    "1de1b9e8-3012-44fb-bb56-7bdd2831fda7/scratchpad/node_modules/jsdom")).VirtualConsole)()
    .on("jsdomError", e => errors.push("jsdomError: " + (e.message || e)))
    .on("error", (...a) => errors.push("console.error: " + a.join(" "))),
});

setTimeout(() => {
  const d = dom.window.document;
  const out = [];
  const fail = m => { out.push("  !!! " + m); process.exitCode = 1; };
  const ok = m => out.push("  ✅ " + m);

  if (errors.length) errors.slice(0, 6).forEach(fail);
  else ok("çalışma zamanı hatası yok");

  const hostId = wantEditor ? "fxCards" : "panelCards";
  // Yayın panelinde Detay Analiz sekmesine geçmek gerekir; düzenleme sürümü
  // zaten oraya açılıyor.
  if (!wantEditor && dom.window.switchTab) dom.window.switchTab("panel");

  const host = d.getElementById(hostId);
  if (!host) return fail(`#${hostId} yok`), console.log(out.join("\n"));

  const cards = host.querySelectorAll(".card");
  if (cards.length) ok(`${cards.length} kart çizildi`);
  else fail(`#${hostId} BOŞ — kart çizilmedi (içerik: ${JSON.stringify(host.innerHTML.slice(0,120))})`);

  const cells = host.querySelectorAll("table.cond .st");
  cells.length ? ok(`${cells.length} hak hücresi`) : fail("hak hücresi yok");

  if (wantEditor) {
    d.getElementById("fxScope")?.querySelectorAll("button").length
      ? ok("yetki seçici dolu") : fail("yetki seçici boş");
    host.querySelectorAll(".fx-ed").length
      ? ok(`${host.querySelectorAll(".fx-ed").length} düzenlenebilir alan işaretli`)
      : fail("düzenlenebilir alan işaretlenmemiş");
    const vis = [...d.querySelectorAll(".tab-btn")].filter(b => !b.hidden).length;
    vis === 1 ? ok("tek sekme açık") : fail(`${vis} sekme açık (1 olmalı)`);
  } else {
    d.getElementById("fixTab")?.hidden ? ok("Düzeltme sekmesi gizli") : fail("Düzeltme sekmesi AÇIK");
    d.getElementById("scoreBtn") ? ok("Skor Ayarı düğmesi var") : fail("Skor Ayarı düğmesi yok");
  }

  console.log(out.join("\n"));
  /* Panelin saat setInterval'i jsdom'u ayakta tutuyor: pencereyi kapatıp
     süreçten çıkmazsak test asla bitmez (ilk denemede 2 dk zaman aşımına
     düştü). */
  try { dom.window.close(); } catch (e) {}
  process.exit(process.exitCode || 0);
}, 1500);
