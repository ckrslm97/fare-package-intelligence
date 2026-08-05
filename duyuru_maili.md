# Duyuru maili — taslak

**Konu:** Fare Package Intelligence (FPI) paneli yayında — rakip paket yapılarını tek ekrandan izleyin

---

Merhaba,

Rakip havayollarının **markalı ücret paketlerini** (branded fares) ve bu paketler arasındaki
**geçiş ücretlerini** tek ekranda karşılaştırabileceğimiz bir panel hazırladık. Panel canlıdır ve
aşağıdaki adresten erişilebilir:

**https://ckrslm97.github.io/fare-package-intelligence/**

Panelin temel sorusu şu: *bir yolcu bir üst pakete geçmek için ne ödüyor ve karşılığında ne
kazanıyor?* Tüm ekranlar nihai bilet fiyatını değil, **paketler arası geçiş ücretini** ve
**paket içeriğini** temel alır.

## Kapsam

- **226 OND** (kalkış–varış çifti), **60 taşıyıcı**, **389 farklı paket**
- **Economy, Business ve First** kabinleri
- **Yaz ve Kış** sezonları için ayrı çekim
- Toplam **6.827 tarife satırı**

## Sayfalar

**Kokpit** — yönetici özeti; dört görünüm içerir:
- *Pazar Konumlanması*: her satır bir pazardır. TK'nın kademe geçiş ücreti, rakip aralığı ve
  pazar ortalamasıyla birlikte tek bantta gösterilir; sağdaki rozet "ucuz / pahalı /
  ortalamada" ve içerik zenginliği yargısını verir.
- *Paket İndeksi*: seçili pazarda TK ile rakiplerin paketleri kademe kademe yan yana.
- *Paket Karşılaştırma*: solda paket içerik skorları, sağda kademe geçiş ücretleri.
- *Senaryo & Öneri*: geçişkenlik verimliliği üzerinden okunan özet çıkarımlar.

**Detay Analiz** — paket paket ham gerçekler. İki mod var: *Koşullar* (her paketin bagaj, koltuk,
yemek, iade, değişiklik gibi haklarını matris olarak gösterir) ve *Geçişkenlik* (aynı kartların
geçiş ücreti odaklı hâli). OND / ülke / bölge ve kabin kırılımı seçilebilir.

**Analitik Analiz** — çapraz kesitler:
- *Heatmap*: taşıyıcı × kademe geçişi ısı haritası; metrik olarak geçiş ücreti, kazanılan hak
  veya skor değişimi seçilebilir.
- *Paket Skor Mukayesesi*: kademe bazında içerik skorları.
- *Geçişkenlik Mukayesesi*: TK ile seçilen bir rakibi aynı geçişte yan yana koyar ve farkı
  cümleyle açıklar.

**Arşiv** — geçmiş veri çekimleri ve ham veriye erişim; filtrelenmiş veri TSV olarak indirilebilir.

**Bilgi Bankası** — her taşıyıcının veriden çıkarılan künyesi (kapsam, kademe sayısı, ortalama
geçiş ücreti, marka listesi), metrik sözlüğü ve metodoloji notları. Kendi gözlemlerinizi not
olarak ekleyebilirsiniz.

Üstteki filtre çubuğu **tüm sayfaları birlikte** etkiler: taşıyıcı, kabin, OND ve sezon her zaman
görünürdedir, coğrafi kırılımlar ve toplama tarihi "More filters" altındadır. Sağ üstteki düğmeyle
aydınlık/karanlık tema arasında geçiş yapılabilir.

## Verinin niteliği hakkında — lütfen dikkate alın

Bu veri **halka açık bir web kaynağından otomatik olarak toplanmaktadır**, doğrudan havayollarının
dağıtım sistemlerinden değil. Bu nedenle:

- Veri **hata ve eksiklik içerebilir**. Bir paketin bir hakkı kaynakta hiç yayınlanmamış olabilir
  ve panelde de görünmez.
- **Her taşıyıcı her rotada listelenmez.** Bazı havayolları (özellikle charter ve bazı düşük
  maliyetli taşıyıcılar) bu kaynakta hiç satılmaz ya da markalı paket yayınlamaz; böyle
  durumlarda ilgili satır boş kalır — bu "o havayolu o hattı uçmuyor" anlamına gelmez.
- Ücretler **belirli tarihlerde alınan anlık görüntülerdir** ve fiyatlar sürekli değişir.
  Rakamları kesin fiyat değil, **büyüklük mertebesi ve konumlanma göstergesi** olarak okuyun.
- Tutarlar tek bir para birimine (USD) çevrilmiştir; kur dönüşümü yaklaşıktır.
- Panel **nihai/satılabilir bilet fiyatı göstermez**; amacı fiyat sorgulamak değil, paket
  yapısını ve geçiş mantığını karşılaştırmaktır.

Ticari bir karara esas teşkil edecek bir bulguda, lütfen ilgili rakamı yayınlayan kaynaktan
teyit edin.

Görüş, hata bildirimi ve geliştirme talepleriniz için bana yazabilirsiniz.

İyi çalışmalar,
Selim
