# Diyanet Awqat Salah API'sinden resmi namaz vakitlerini ceker ve uygulamanin okudugu statik dosyalari uretir.
#
# Neden boyle: API kotasi dar (yillik veri konum basina ayda 10 istek), bu yuzden uygulama Diyanet'e dogrudan
# sormaz. Bu betik yilda bir (ve yeni yil yaklasinca) calisir, her ilce icin TEK istekle butun yili alir ve
# birizapp.com.tr'ye konacak dosyalari yazar. Uygulama dosya yoksa Aladhan + duzeltme tablosuna duser.
#
# Kullanim (kimlik bilgileri ortamdan; depoya ASLA yazilmaz):
#   set DIYANET_EMAIL=...  &  set DIYANET_PASSWORD=...
#   python cek.py yerler                 -> cikti/vakit/yerler.json (Turkiye'deki il ve ilceler, koordinatlariyla)
#   python cek.py yil 2027 [--sinir N]   -> cikti/vakit/2027/<ilceId>.json (kaldigi yerden devam eder)
#   python cek.py icerik                 -> cikti/icerik/<tarih>.json + cikti/icerik/bugun.json (gunun ayet/hadis/duasi;
#                                           tarih Turkiye saatiyle, o gunun dosyasi varsa API'ye sormaz)
#   CIKTI_DIR=<klasor> verilirse cikti/ yerine oraya yazar (biriz-web Actions: CIKTI_DIR=.)
#   python cek.py kota                   -> kalan kota
#   python cek.py ornek <dosya.json>     -> API cevabini (elle kaydedilmis) uygulama bicimine cevirip yazdirir (kimliksiz deneme)
#
# Uygulamanin okudugu bicim (bunu degistirirsen lib/services/diyanet_static_service.dart'i de degistir):
#   yerler.json  : {"surum": 1, "yerler": [{"id": 9541, "ad": "İstanbul", "il": "İstanbul", "lat": 41.0, "lng": 28.9}, ...]}
#   <yil>/<id>.json : {"surum": 1, "id": 9541, "yil": 2027,
#                      "gunler": [["2027-01-01", "06:50", "08:22", "13:12", "15:32", "17:53", "19:19", "23 Recep 1448"], ...]}
#   Sira: tarih, imsak, gunes, ogle, ikindi, aksam, yatsi, hicri tarih (uzun).
import json, os, sys, time, datetime, http.client, urllib.request, urllib.error

TABAN = "https://awqatsalah.diyanet.gov.tr"
BURASI = os.path.dirname(os.path.abspath(__file__))
# Actions'ta CIKTI_DIR=. verilir: dosyalar dogrudan sitenin vakit/ ve icerik/ klasorune yazilir, "var mi" kontrolu
# sitedeki dosyaya bakar (cikti/ orada yok; yoksa her ay 869 ilce bastan cekilirdi).
CIKTI = os.environ.get("CIKTI_DIR") or os.path.join(BURASI, "cikti")
# Turkiye UTC+3, yaz saati yok (2016'dan beri). Actions UTC'de calisir; date.today() gece yarisindan sonra dunu verirdi.
TR = datetime.timezone(datetime.timedelta(hours=3))
SURUM = 1
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_token = {"deger": None, "zaman": 0.0}


def _istek(yol, govde=None, yetki=True):
    basliklar = {"Content-Type": "application/json", "Accept": "application/json"}
    if yetki:
        basliklar["Authorization"] = "Bearer " + _giris()
    veri = json.dumps(govde).encode("utf-8") if govde is not None else None
    req = urllib.request.Request(TABAN + yol, data=veri, headers=basliklar, method="POST" if veri else "GET")
    for deneme in range(4):
        time.sleep(0.5)  # ardisik isteklere nefes payi
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            govde_metni = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 or "kota" in govde_metni.lower() or "quota" in govde_metni.lower():
                raise SystemExit("KOTA DOLDU (%s): %s" % (yol, govde_metni))
            if e.code in (401, 403) and yetki and deneme == 0:
                _token["deger"] = None  # suresi dolmus olabilir, bir kez yeniden gir
                basliklar["Authorization"] = "Bearer " + _giris()
                req = urllib.request.Request(TABAN + yol, data=veri, headers=basliklar, method=req.get_method())
                continue
            raise SystemExit("HATA %d %s: %s" % (e.code, yol, govde_metni))
        except (OSError, http.client.HTTPException, ValueError) as e:  # ValueError: 200 ama bos govde (10-20, ara sira)
            # ag koptu (sunucu ardisik isteklerde baglantiyi sifirliyor, 10-20): bekle, tekrar dene
            time.sleep(5 + deneme * 10)
    raise SystemExit("AG: %s 4 denemede alinamadi" % yol)


def _giris():
    # Erisim belirteci 45 dk gecerli; 40 dk'da bir yenile.
    if _token["deger"] and time.time() - _token["zaman"] < 40 * 60:
        return _token["deger"]
    e, p = os.environ.get("DIYANET_EMAIL"), os.environ.get("DIYANET_PASSWORD")
    if not e or not p:
        raise SystemExit("DIYANET_EMAIL ve DIYANET_PASSWORD ortam degiskenleri gerekli.")
    cevap = _istek("/Auth/Login", {"email": e, "password": p}, yetki=False)
    _token["deger"] = cevap["data"]["accessToken"]
    _token["zaman"] = time.time()
    return _token["deger"]


def _veri(cevap):
    return cevap.get("data") if isinstance(cevap, dict) else cevap


def _yaz(yol, veri):
    os.makedirs(os.path.dirname(yol), exist_ok=True)
    gecici = yol + ".tmp"
    with open(gecici, "w", encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(gecici, yol)  # yarim dosya birakma


def _tarih(gun):
    """API'nin miladi tarih alanlarindan YYYY-AA-GG cikarir (ISO ya da GG.AA.YYYY)."""
    for alan in ("gregorianDateShortIso8601", "gregorianDateLongIso8601", "gregorianDateShort"):
        s = (gun.get(alan) or "").strip()
        if len(s) >= 10 and s[4] == "-":
            return s[:10]
        if len(s) >= 10 and s[2] == ".":
            return "%s-%s-%s" % (s[6:10], s[3:5], s[0:2])
    raise ValueError("tarih okunamadi: %r" % {k: gun.get(k) for k in gun if "regorian" in k})


def _saat(s):
    s = (s or "").strip()[:5]
    if len(s) != 5 or s[2] != ":":
        raise ValueError("saat okunamadi: %r" % s)
    return s


def uygulama_bicimi(ham_gunler, kimlik, yil):
    gunler = []
    for g in ham_gunler:
        gunler.append([_tarih(g), _saat(g["fajr"]), _saat(g["sunrise"]), _saat(g["dhuhr"]), _saat(g["asr"]),
                       _saat(g["maghrib"]), _saat(g["isha"]), (g.get("hijriDateLong") or "").strip()])
    gunler.sort(key=lambda x: x[0])
    return {"surum": SURUM, "id": kimlik, "yil": yil, "gunler": gunler}


def yerler():
    ulkeler = _veri(_istek("/api/Place/Countries"))
    tr = next(u for u in ulkeler if (u.get("code") or "").upper() in ("TR", "TUR") or "TÜRK" in (u.get("name") or "").upper())
    iller = _veri(_istek("/api/Place/States/%d" % tr["id"]))
    print("Turkiye kimligi:", tr["id"], "| il:", len(iller))
    # Koordinat icin butun sehirlerin listesi tek istekte geliyor (CityModel: id, name, latitude, longitude).
    koordinat = {c["id"]: c for c in _veri(_istek("/api/Place/Cities")) if "latitude" in c}
    sonuc = []
    for il in iller:
        for ilce in _veri(_istek("/api/Place/Cities/%d" % il["id"])):
            k = koordinat.get(ilce["id"], {})
            sonuc.append({"id": ilce["id"], "ad": ilce["name"], "il": il["name"],
                          "lat": k.get("latitude"), "lng": k.get("longitude")})
        time.sleep(0.5)
    eksik = sum(1 for y in sonuc if y["lat"] is None)
    print("ilce:", len(sonuc), "| koordinati eksik:", eksik)
    _yaz(os.path.join(CIKTI, "vakit", "yerler.json"), {"surum": SURUM, "yerler": sonuc})


def yil(y, sinir=None):
    yol = os.path.join(CIKTI, "vakit", "yerler.json")
    if not os.path.exists(yol):
        raise SystemExit("Once: python cek.py yerler")
    hepsi = json.load(open(yol, encoding="utf-8"))["yerler"]
    yapilan = 0
    for yer in hepsi:
        hedef = os.path.join(CIKTI, "vakit", str(y), "%d.json" % yer["id"])
        if os.path.exists(hedef):
            continue  # kaldigi yerden devam: her konum icin yilda tek istek
        if sinir is not None and yapilan >= sinir:
            break
        ham = _veri(_istek("/api/PrayerTime/DateRange", {
            "cityId": yer["id"], "startDate": "%d-01-01T00:00:00" % y, "endDate": "%d-12-31T00:00:00" % y}))
        veri = uygulama_bicimi(ham, yer["id"], y)
        if len(veri["gunler"]) < 365:
            print("UYARI:", yer["il"], yer["ad"], "yalniz", len(veri["gunler"]), "gun")
        _yaz(hedef, veri)
        yapilan += 1
        print("%s / %s: %d gun" % (yer["il"], yer["ad"], len(veri["gunler"])))
        time.sleep(1.0)
    print("bu kosuda:", yapilan, "konum")


def icerik():
    simdi = datetime.datetime.now(TR)
    bugun = simdi.date().isoformat()
    if os.path.exists(os.path.join(CIKTI, "icerik", bugun + ".json")):
        print("bugunun icerigi zaten var:", bugun)  # gunde 3 deneme; ilk basari yeter, kotayi harcama
        return
    v = _veri(_istek("/api/DailyContent"))
    if v.get("dayOfYear") not in (None, simdi.timetuple().tm_yday):
        print("UYARI: API'nin gunu", v.get("dayOfYear"), "bizim gun", simdi.timetuple().tm_yday)
    kayit = {"surum": SURUM, "tarih": bugun,
             "ayet": v.get("verse"), "ayetKaynak": v.get("verseSource"),
             "hadis": v.get("hadith"), "hadisKaynak": v.get("hadithSource"),
             "dua": v.get("pray"), "duaKaynak": v.get("praySource")}
    _yaz(os.path.join(CIKTI, "icerik", bugun + ".json"), kayit)
    _yaz(os.path.join(CIKTI, "icerik", "bugun.json"), kayit)
    print(json.dumps(kayit, ensure_ascii=False)[:300])


if __name__ == "__main__":
    a = sys.argv[1:] or ["?"]
    if a[0] == "yerler":
        yerler()
    elif a[0] == "yil":
        yil(int(a[1]), int(a[a.index("--sinir") + 1]) if "--sinir" in a else None)
    elif a[0] == "icerik":
        icerik()
    elif a[0] == "kota":
        print(json.dumps(_veri(_istek("/api/Quota/My")), ensure_ascii=False, indent=1))
    elif a[0] == "ornek":
        ham = _veri(json.load(open(a[1], encoding="utf-8")))
        print(json.dumps(uygulama_bicimi(ham, 0, 0), ensure_ascii=False)[:600])
    else:
        print(__doc__ or open(__file__, encoding="utf-8").read().split("import json")[0])
