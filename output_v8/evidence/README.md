# v8 Screenshot Verification Report

Her taşıyıcı için aynı rota+tarihte Ubfly yeniden yüklendi; canlı paket adları/fiyatları
çekilen veriyle birebir karşılaştırıldı, panel ekran görüntüleri kaydedildi.

**36 örnek — OK: 35, sorun: 1** (OK_PRICE_DRIFT = isimler birebir, fiyat gün içinde güncellenmiş — dinamik fiyatlama; NO_CARRIER_TODAY = taşıyıcı o an o rotada listelenmiyor)

| Taşıyıcı | OND | Arama | Sonuç | Kanıt |
|---|---|---|---|---|
| AA | JFK-DEL | Economy | OK | AA_JFKDEL_eco.png |
| AC | YYZ-DEL | Economy | OK | AC_YYZDEL_eco.png |
| AC | YYZ-DEL | Business | OK | AC_YYZDEL_bus.png |
| AF | YYZ-DEL | Economy | OK | AF_YYZDEL_eco.png |
| AF | YYZ-DEL | Business | OK_PRICE_DRIFT | AF_YYZDEL_bus.png |
| AI | YYZ-DEL | Economy | OK | AI_YYZDEL_eco.png |
| AI | YYZ-DEL | Business | OK | AI_YYZDEL_bus.png |
| BA | LHR-SIN | Economy | OK | BA_LHRSIN_eco.png |
| BA | LHR-SIN | Business | OK | BA_LHRSIN_bus.png |
| BR | TPE-JFK | Economy | OK | BR_TPEJFK_eco.png |
| CA | WAW-BKK | Economy | OK | CA_WAWBKK_eco.png |
| CI | HKG-JFK | Economy | OK_PRICE_DRIFT | CI_HKGJFK_eco.png |
| CX | YVR-DEL | Economy | OK | CX_YVRDEL_eco.png |
| DL | TPE-JFK | Economy | OK | DL_TPEJFK_eco.png |
| EK | LHR-KUL | Economy | OK | EK_LHRKUL_eco.png |
| EK | LHR-KUL | Business | OK | EK_LHRKUL_bus.png |
| EY | LHR-KUL | Economy | OK | EY_LHRKUL_eco.png |
| EY | LHR-KUL | Business | OK | EY_LHRKUL_bus.png |
| GF | ISB-LGW | Economy | OK | GF_ISBLGW_eco.png |
| GF | ISB-LGW | Business | OK | GF_ISBLGW_bus.png |
| KE | JFK-HKG | Economy | OK | KE_JFKHKG_eco.png |
| KL | AMS-BKK | Economy | OK | KL_AMSBKK_eco.png |
| KL | AMS-BKK | Business | OK | KL_AMSBKK_bus.png |
| LH | DEL-YVR | Economy | OK | LH_DELYVR_eco.png |
| LH | DEL-YVR | Business | OK | LH_DELYVR_bus.png |
| MH | LHR-KUL | Economy | OK | MH_LHRKUL_eco.png |
| NH | HKG-JFK | Economy | OK | NH_HKGJFK_eco.png |
| QF | LHR-SIN | Economy | OK | QF_LHRSIN_eco.png |
| QR | LHR-SIN | Economy | OK | QR_LHRSIN_eco.png |
| QR | LHR-SIN | Business | OK | QR_LHRSIN_bus.png |
| SQ | LHR-SIN | Economy | OK | SQ_LHRSIN_eco.png |
| SV | LHE-LHR | Economy | OK | SV_LHELHR_eco.png |
| SV | LHE-LHR | Business | OK | SV_LHELHR_bus.png |
| TK | DEL-SFO | Economy | NO_CARRIER_TODAY | - |
| UA | HKG-YYZ | Economy | OK | UA_HKGYYZ_eco.png |
| UA | HKG-YYZ | Business | OK_PRICE_DRIFT | UA_HKGYYZ_bus.png |