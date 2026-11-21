
import re
import html
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, unquote, urlparse, parse_qs
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"
ISTANBUL = ZoneInfo("Europe/Istanbul")

DAYS = 7
PAGE_TIMEOUT = 60000
DATE_WAIT_MS = 1200
CHANNEL_WAIT_MS = 1200
MIN_CHANNELS = 30
MIN_PROGRAMS = 100

CATEGORY_PAGES = [
    ("Tüm Kanallar", "/canli-tv"),
    ("Diğer", "/canli-tv/diger"),
    ("Global", "/canli-tv/global"),
    ("Ulusal", "/canli-tv/ulusal"),
    ("Müzik", "/canli-tv/muzik"),
    ("Yaşam-Stil", "/canli-tv/yasam-stil"),
    ("Dizi", "/canli-tv/dizi"),
    ("Spor", "/canli-tv/spor"),
    ("Haber", "/canli-tv/haber"),
    ("Belgesel", "/canli-tv/belgesel"),
    ("Çocuk", "/canli-tv/cocuk"),
    ("Sinema", "/canli-tv/sinema"),
]

BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
    "TİVİBU TANITIM SAYFASI",
}

# Tivibu Tüm Kanallar sayfasından alınan gerçek kanal kodları.
# Eşleştirme artık kanal logosunun sırasına göre yapılmaz.
CHANNELS_BY_CODE = {
    "ch00000000000000001358": "COP31",
    "ch00000000000000001481": "BENİM KANALIM",
    "ch00000000000000002187": "TARİH TV",
    "ch00000000000000001258": "SİNEMA TV",
    "ch00000000000000002783": "SİNEMA 2",
    "ch00000000000000001259": "SİNEMA YERLİ",
    "ch00000000000000002784": "SİNEMA YERLİ 2",
    "ch00000000000000001263": "SİNEMA AKSİYON",
    "ch00000000000000002803": "SİNEMA AKSİYON 2",
    "ch00000000000000001281": "SİNEMA KOMEDİ",
    "ch00000000000000002785": "SİNEMA KOMEDİ 2",
    "ch00000000000000001271": "SİNEMA AİLE",
    "ch00000000000000002786": "SİNEMA AİLE 2",
    "ch00000000000000001262": "SİNEMA 1001",
    "ch00000000000000002813": "SİNEMA 1002",
    "ch00000000000000002905": "FILMSCREEN INTERNATIONAL",
    "ch00000000000000002945": "Cosmo EN",
    "ch00000000000000001062": "BBC FIRST",
    "ch00000000000000001441": "FX",
    "ch00000000000000002389": "EPIC DRAMA",
    "ch00000000000000001266": "TRT 1",
    "ch00000000000000001166": "KANAL D",
    "ch00000000000000001017": "ATV",
    "ch00000000000000001230": "SHOW TV",
    "ch00000000000000001162": "NOW",
    "ch00000000000000001170": "STAR TV",
    "ch00000000000000001165": "KANAL 7",
    "ch00000000000000001351": "TV8",
    "ch00000000000000001401": "360",
    "ch00000000000000001064": "BEYAZ TV",
    "ch00000000000000001307": "TRT HABER",
    "ch00000000000000001221": "NTV",
    "ch00000000000000001021": "A HABER",
    "ch00000000000000001001": "24",
    "ch00000000000000001092": "CNN TÜRK",
    "ch00000000000000001159": "HABERTÜRK",
    "ch00000000000000001081": "BLOOMBERG HT",
    "ch00000000000000001360": "ÜLKE TV",
    "ch00000000000000001361": "TVNET",
    "ch00000000000000002641": "CNBC-e",
    "ch00000000000000001260": "TGRT HABER",
    "ch00000000000000001024": "A PARA",
    "ch00000000000000001027": "AKİT TV",
    "ch00000000000000001156": "HABER GLOBAL",
    "ch00000000000000001268": "TV 100",
    "ch00000000000000001071": "BENGÜTÜRK",
    "ch00000000000000001125": "DİYANET TV",
    "ch00000000000000001026": "A2",
    "ch00000000000000001291": "TV2",
    "ch00000000000000001310": "TV 8,5",
    "ch00000000000000001301": "TLC",
    "ch00000000000000001124": "DMAX",
    "ch00000000000000001302": "TRT 2",
    "ch00000000000000001371": "VAV TV",
    "ch00000000000000002531": "TÜRKHABER TV",
    "ch00000000000000001981": "TV4",
    "ch00000000000000001183": "KON TV",
    "ch00000000000000002631": "EKOL TV",
    "ch00000000000000001359": "ULUSAL 1",
    "ch00000000000000001462": "EKOTÜRK",
    "ch00000000000000002691": "GZT TV",
    "ch00000000000000002287": "TV 5",
    "ch00000000000000001241": "SEMERKAND TV",
    "ch00000000000000002075": "HALK TV",
    "ch00000000000000002267": "SZC TV",
    "ch00000000000000001131": "EUROSPORT 1",
    "ch00000000000000001141": "EUROSPORT 2",
    "ch00000000000000001356": "TİVİBU SPOR 1",
    "ch00000000000000001270": "TİVİBU SPOR 2",
    "ch00000000000000001357": "TİVİBU SPOR 3",
    "ch00000000000000001971": "TİVİBU SPOR 4",
    "ch00000000000000001355": "TİVİBU SPOR",
    "ch00000000000000001025": "A SPOR",
    "ch00000000000000001461": "TRT SPOR",
    "ch00000000000000001341": "TRT SPOR YILDIZ",
    "ch00000000000000001151": "FB TV",
    "ch00000000000000002681": "HT SPOR HD",
    "ch00000000000000001161": "FIGHT NETWORK",
    "ch00000000000000002915": "COSMO SPORTS",
    "ch00000000000000001303": "TRT 3 SPOR",
    "ch00000000000000001304": "TRT BELGESEL",
    "ch00000000000000001160": "HABİTAT TV",
    "ch00000000000000002188": "TARİH TV",
    "ch00000000000000001201": "NATIONAL GEOGRAPHIC",
    "ch00000000000000001211": "NATIONAL GEOGRAPHIC WILD",
    "ch00000000000000001111": "DISCOVERY CHANNEL",
    "ch00000000000000001171": "ID",
    "ch00000000000000001891": "VIASAT HISTORY",
    "ch00000000000000001881": "LOVE NATURE",
    "ch00000000000000001061": "BBC EARTH",
    "ch00000000000000001213": "TRT ÇOCUK",
    "ch00000000000000001187": "MİNİKA ÇOCUK",
    "ch00000000000000001186": "MİNİKA GO",
    "ch00000000000000002298": "TRT DİYANET ÇOCUK",
    "ch00000000000000002833": "SPACETOON",
    "ch00000000000000001091": "CARTOONITO",
    "ch00000000000000001123": "DISNEY JUNIOR",
    "ch00000000000000001068": "CARTOON NETWORK",
    "ch00000000000000002247": "MOONBUG",
    "ch00000000000000002975": "BLIPPI & FRIENDS",
    "ch00000000000000001019": "BABYTV",
    "ch00000000000000002541": "NICKELODEON",
    "ch00000000000000001190": "NICKTOONS",
    "ch00000000000000002701": "NICK JR",
    "ch00000000000000001093": "DA VINCI",
    "ch00000000000000002925": "TRT GENÇ",
    "ch00000000000000001308": "TRT TÜRK",
    "ch00000000000000001267": "TRT AVAZ",
    "ch00000000000000001321": "TRT KURDİ",
    "ch00000000000000002955": "DOST TV",
    "ch00000000000000002731": "TELE 1",
    "ch00000000000000002773": "TİVİ 6",
    "ch00000000000000002721": "AS TV",
    "ch00000000000000002843": "TYT TÜRK",
    "ch00000000000000002823": "LALEGÜL",
    "ch00000000000000002853": "FLASH TV",
    "ch00000000000000001331": "TRT MÜZİK",
    "ch00000000000000001223": "NUMBER1 TÜRK",
    "ch00000000000000001095": "DREAM TÜRK TV",
    "ch00000000000000001231": "POWER TV",
    "ch00000000000000001225": "POWER TÜRK",
    "ch00000000000000001070": "CNN INTERNATIONAL",
    "ch00000000000000001013": "AL JAZEERA INTERNATIONAL",
    "ch00000000000000001309": "TRT WORLD",
    "ch00000000000000001101": "CNBC",
    "ch00000000000000001063": "BBC NEWS",
    "ch00000000000000001065": "BLOOMBERG",
    "ch00000000000000001030": "ANEWS",
    "ch00000000000000001234": "TV5 MONDE",
    "ch00000000000000001189": "NHK WORLD-JAPAN",
    "ch00000000000000001094": "DEUTSCHE WELLE",
    "ch00000000000000001306": "TRT EL ARABIA",
    "ch00000000000000001028": "AL JAZEERA ARABIC",
    "ch00000000000000002985": "AL JAZEERA NOW",
    "ch00000000000000001168": "SAUDI QURAN",
    "ch00000000000000001921": "AL SUNNAH",
    "ch00000000000000001226": "RT ARABIC",
    "ch00000000000000001154": "FRANCE 24",
    "ch00000000000000001233": "TRT EBA",
    "ch00000000000000001354": "TÜRKÇE POP",
    "ch00000000000000001269": "TÜRKÇE SLOW",
    "ch00000000000000001353": "TÜRK HALK MÜZİĞİ",
    "ch00000000000000001235": "TÜRK SANAT MÜZİĞİ",
    "ch00000000000000001265": "TAŞ PLAK",
    "ch00000000000000001112": "DİNİ MUSİKİ",
    "ch00000000000000001016": "ARABESK",
    "ch00000000000000001022": "90’ LAR",
    "ch00000000000000001293": "YABANCI POP",
    "ch00000000000000001342": "YABANCI ROCK",
    "ch00000000000000001237": "YABANCI SLOW",
    "ch00000000000000001163": "FİLM MÜZİKLERİ",
    "ch00000000000000001224": "OLDIES",
    "ch00000000000000001066": "BLUES",
    "ch00000000000000001172": "JAZZ",
    "ch00000000000000001191": "KLASİK MÜZİK",
    "ch00000000000000001167": "RETRO",
    "ch00000000000000001041": "AKUSTİK",
    "ch00000000000000001184": "LOUNGE",
    "ch00000000000000001185": "MUTLU",
    "ch00000000000000001173": "ÇALIŞIRKEN",
    "ch00000000000000001236": "TİVİBU ÇOCUK",
}


EXTRACT_JS = r"""() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();

  const anchors = [...document.querySelectorAll('a[href*="/rv?i="]')];
  const rows = [];

  for (const a of anchors) {
    let raw = a.getAttribute('href') || '';
    let decoded = raw;

    try {
      decoded = decodeURIComponent(raw);
    } catch (e) {}

    const match = decoded.match(/ch\d{20}/i);
    if (!match) continue;

    const channelCode = match[0].toLowerCase();

    const titleEl = a.querySelector('.programTitle');
    const startEl = a.querySelector('.startTime');
    const finishEl = a.querySelector('.finishTime');

    if (!titleEl || !startEl || !finishEl) continue;

    const title = norm(titleEl.innerText || titleEl.textContent);
    const start = norm(startEl.innerText || startEl.textContent);
    const finish = norm(finishEl.innerText || finishEl.textContent);

    if (!title || !/^\d{1,2}:\d{2}$/.test(start) ||
        !/^\d{1,2}:\d{2}$/.test(finish)) continue;

    rows.push({
      code: channelCode,
      title,
      start,
      finish
    });
  }

  const unique = new Map();

  for (const row of rows) {
    const key = [
      row.code,
      row.title,
      row.start,
      row.finish
    ].join('|');

    if (!unique.has(key)) unique.set(key, row);
  }

  return {
    rows: [...unique.values()],
    anchorCount: anchors.length,
    programCount: unique.size,
    codes: [...new Set([...unique.values()].map(x => x.code))]
  };
}"""


def clean_text(value):
    value = html.unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def slugify(value):
    value = clean_text(value)

    replacements = {
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o",
        "ş": "s", "ü": "u", "â": "a", "î": "i",
        "û": "u",
    }

    for old, new in replacements.items():
        value = value.replace(old, new).replace(old.upper(), new)

    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        c for c in value if not unicodedata.combining(c)
    ).lower()

    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value or "channel"


def parse_clock(value):
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", clean_text(value))
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def date_label(day, today):
    if day == today:
        return "Bugün"
    if day == today + timedelta(days=1):
        return "Yarın"
    return day.strftime("%d.%m.%Y")


def open_category(page, category_name, path):
    url = urljoin(BASE_URL, path)

    print(f"\nKategori açılıyor: {category_name}")
    print(f"Adres: {url}")

    page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT
    )

    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(1000)


def select_date(page, label):
    # Tarih seçeneğini JavaScript ile tıkla.
    result = page.evaluate(
        """(label) => {
            const nodes = [...document.querySelectorAll('a,button,li,span,div')];
            const el = nodes.find(x =>
                (x.innerText || '').trim() === label &&
                x.getClientRects().length > 0
            );

            if (!el) return false;

            el.click();
            return true;
        }""",
        label
    )

    if not result:
        raise RuntimeError(f"Tarih seçeneği bulunamadı: {label}")

    page.wait_for_timeout(DATE_WAIT_MS)


def assign_times(items, selected_day):
    """
    items: aynı kanala ait title/start/finish kayıtları.
    Saat sırasındaki gece yarısı geçişlerini dikkate alır.
    """
    parsed = []

    for item in items:
        start_clock = parse_clock(item["start"])
        finish_clock = parse_clock(item["finish"])

        if not start_clock or not finish_clock:
            continue

        parsed.append({
            "title": clean_text(item["title"]),
            "sh": start_clock[0],
            "sm": start_clock[1],
            "eh": finish_clock[0],
            "em": finish_clock[1],
        })

    if not parsed:
        return []

    first_minutes = parsed[0]["sh"] * 60 + parsed[0]["sm"]

    has_rollover = any(
        (
            parsed[i]["sh"] * 60 + parsed[i]["sm"]
        ) < (
            parsed[i - 1]["sh"] * 60 + parsed[i - 1]["sm"]
        )
        for i in range(1, len(parsed))
    )

    current_day = (
        selected_day - timedelta(days=1)
        if first_minutes >= 18 * 60 and has_rollover
        else selected_day
    )

    previous_minutes = None
    result = []

    for item in parsed:
        start_minutes = item["sh"] * 60 + item["sm"]

        if (
            previous_minutes is not None
            and start_minutes < previous_minutes
        ):
            current_day += timedelta(days=1)

        start = datetime(
            current_day.year,
            current_day.month,
            current_day.day,
            item["sh"],
            item["sm"],
            tzinfo=ISTANBUL
        )

        finish_minutes = item["eh"] * 60 + item["em"]

        end_day = (
            current_day + timedelta(days=1)
            if finish_minutes <= start_minutes
            else current_day
        )

        stop = datetime(
            end_day.year,
            end_day.month,
            end_day.day,
            item["eh"],
            item["em"],
            tzinfo=ISTANBUL
        )

        if (
            stop > start
            and (stop - start).total_seconds() <= 24 * 3600
        ):
            result.append({
                "title": item["title"],
                "start": start,
                "stop": stop
            })

        previous_minutes = start_minutes

    return result


def collect_all(page, today):
    all_channels = set()
    all_programs = {}

    unknown_codes = set()
    category_channel_counts = {}

    window_start = datetime(
        today.year, today.month, today.day,
        0, 0, tzinfo=ISTANBUL
    )

    end_day = today + timedelta(days=DAYS)

    window_end = datetime(
        end_day.year, end_day.month, end_day.day,
        0, 0, tzinfo=ISTANBUL
    )

    print(f"\nSabit kanal kodu sayısı: {len(CHANNELS_BY_CODE)}")
    print("Programlar doğrudan kanal koduyla eşleştirilecek.")

    for category_name, path in CATEGORY_PAGES:
        open_category(page, category_name, path)

        category_channels = set()

        for offset in range(DAYS):
            selected_day = today + timedelta(days=offset)
            label = date_label(selected_day, today)

            print(f"  {label} taranıyor...")

            try:
                select_date(page, label)
            except Exception as exc:
                print(f"  TARİH HATASI: {exc}")
                continue

            extracted = page.evaluate(EXTRACT_JS)
            rows = extracted.get("rows", [])

            print(
                f"    Program bağlantısı: {extracted.get('anchorCount', 0)}"
                f" | Geçerli kayıt: {len(rows)}"
            )

            if not rows:
                print("    UYARI: Bu tarihte program kaydı bulunamadı.")
                continue

            grouped = {}

            for row in rows:
                code = clean_text(row.get("code")).lower()

                channel_name = CHANNELS_BY_CODE.get(code)

                if not channel_name:
                    unknown_codes.add(code)
                    continue

                if channel_name.upper() in BAD_CHANNELS:
                    continue

                grouped.setdefault(channel_name, []).append(row)

            day_new_count = 0

            for channel_name, items in grouped.items():
                programs = assign_times(items, selected_day)

                if not programs:
                    continue

                category_channels.add(channel_name)
                all_channels.add(channel_name)

                for program in programs:
                    if (
                        program["stop"] <= window_start
                        or program["start"] >= window_end
                    ):
                        continue

                    key = (
                        channel_name.casefold(),
                        program["title"].casefold(),
                        program["start"],
                        program["stop"],
                    )

                    if key not in all_programs:
                        all_programs[key] = {
                            "channel_name": channel_name,
                            **program
                        }
                        day_new_count += 1

            print(
                f"    Kodla eşleşen kanal: {len(grouped)}"
                f" | Yeni program: {day_new_count}"
            )

        category_channel_counts[category_name] = len(category_channels)

        print(
            f"  {category_name}: "
            f"{len(category_channels)} kanal bulundu."
        )

    if unknown_codes:
        print("\nEŞLEŞMEYEN KANAL KODLARI:")
        for code in sorted(unknown_codes):
            print(f"  {code}")

    return (
        sorted(all_channels, key=str.casefold),
        list(all_programs.values())
    )


def build_xml(channel_names, programs):
    used_ids = set()
    channel_ids = {}

    for name in sorted(channel_names, key=str.casefold):
        base = slugify(name)
        channel_id = base
        suffix = 2

        while channel_id in used_ids:
            channel_id = f"{base}_{suffix}"
            suffix += 1

        used_ids.add(channel_id)
        channel_ids[name] = channel_id

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL,
        }
    )

    for name in sorted(channel_names, key=str.casefold):
        channel = ET.SubElement(
            root,
            "channel",
            {"id": channel_ids[name]}
        )

        display = ET.SubElement(
            channel,
            "display-name",
            {"lang": "tr"}
        )
        display.text = name

    for item in sorted(
        programs,
        key=lambda x: (
            x["start"],
            x["channel_name"].casefold(),
            x["stop"]
        )
    ):
        programme = ET.SubElement(
            root,
            "programme",
            {
                "channel": channel_ids[item["channel_name"]],
                "start": item["start"].strftime("%Y%m%d%H%M%S %z"),
                "stop": item["stop"].strftime("%Y%m%d%H%M%S %z"),
            }
        )

        title = ET.SubElement(
            programme,
            "title",
            {"lang": "tr"}
        )
        title.text = item["title"]

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    temp_file = OUTPUT_FILE + ".tmp"

    tree.write(
        temp_file,
        encoding="utf-8",
        xml_declaration=True
    )

    # Geçici XML'i doğrula; bozuksa eski XML'e dokunma.
    ET.parse(temp_file)

    import os
    os.replace(temp_file, OUTPUT_FILE)


def validate_data(channel_names, programs):
    if len(channel_names) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(channel_names)} kanal bulundu. "
            f"En az {MIN_CHANNELS} kanal bekleniyordu. "
            "Mevcut XML korunuyor."
        )

    if len(programs) < MIN_PROGRAMS:
        raise RuntimeError(
            f"Yalnızca {len(programs)} program bulundu. "
            f"En az {MIN_PROGRAMS} bekleniyordu. "
            "Mevcut XML korunuyor."
        )

    day_count = len({
        p["start"].date()
        for p in programs
    })

    if day_count < 6:
        raise RuntimeError(
            f"Yalnızca {day_count} yayın günü bulundu. "
            "Mevcut XML korunuyor."
        )


def main():
    started = time.time()
    today = datetime.now(ISTANBUL).date()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG - GERÇEK KANAL KODLARI")
    print(f"Kaynak: {LIVE_TV_URL}")
    print(f"Başlangıç: {today.strftime('%d.%m.%Y')}")
    print(f"Kategori sayısı: {len(CATEGORY_PAGES)}")
    print(f"Kanal kodu sayısı: {len(CHANNELS_BY_CODE)}")
    print("=" * 68)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        page = browser.new_page(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1440,
                "height": 1100
            }
        )

        page.set_default_timeout(PAGE_TIMEOUT)

        channel_names, programs = collect_all(
            page,
            today
        )

        browser.close()

    validate_data(channel_names, programs)

    build_xml(
        channel_names,
        programs
    )

    days = sorted({
        p["start"].date()
        for p in programs
    })

    print("\n" + "=" * 68)
    print("7 GÜNLÜK EPG OLUŞTURULDU")
    print(f"Toplam kanal: {len(channel_names)}")
    print(f"Toplam program: {len(programs)}")
    print(f"Program tarihi sayısı: {len(days)}")
    print(f"İlk tarih: {days[0]}")
    print(f"Son tarih: {days[-1]}")
    print(f"Dosya: {OUTPUT_FILE}")
    print(f"Süre: {time.time() - started:.1f} saniye")
    print("=" * 68)


if __name__ == "__main__":
    main()
