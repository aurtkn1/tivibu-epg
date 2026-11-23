
import re
import html
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"
ISTANBUL = ZoneInfo("Europe/Istanbul")
DAYS = 7
PAGE_TIMEOUT = 60000
DATE_WAIT_MS = 1100
MIN_CHANNELS = 30

CATEGORY_PAGES = [
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

# Kanal kodu üzerinden kesin adlandırma.
# Logo sırası eşleştirmesi kullanılmaz.
CHANNEL_NAME_OVERRIDES = {
    "ch00000000000000002843": "TYT TÜRK",
    "ch00000000000000001981": "TV4",
    "ch00000000000000002267": "SZC TV",
    "ch00000000000000001308": "TRT TÜRK",
    "ch00000000000000001267": "TRT AVAZ",
    "ch00000000000000002925": "TRT GENÇ",
    "ch00000000000000002773": "TİVİ 6",
    "ch00000000000000001070": "CNN INTERNATIONAL",
    "ch00000000000000001063": "BBC NEWS",
    "ch00000000000000001013": "AL JAZEERA INTERNATIONAL",
    "ch00000000000000001101": "CNBC",
    "ch00000000000000001309": "TRT WORLD",

    "ch00000000000000001111": "DISCOVERY CHANNEL",
    "ch00000000000000001201": "NATIONAL GEOGRAPHIC",
    "ch00000000000000001211": "NATIONAL GEOGRAPHIC WILD",
    "ch00000000000000001881": "LOVE NATURE",
    "ch00000000000000001891": "VIASAT HISTORY",

    # AS TV: Tivibu yayın bağlantısındaki programid/contentid ile doğrulandı.
    "ch00000000000000002721": "AS TV",

    "ch00000000000000001061": "BBC EARTH",
    "ch00000000000000001171": "ID",
}

BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
    "TİVİBU TANITIM SAYFASI",
}

TIME_RE = re.compile(
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})\s*(?:→|➜|[-–])\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})"
)

CATEGORY_RE = re.compile(
    r"\s+(?:Yaşam|Film|Dizi|Spor|Haber|Çocuk|Müzik|Belgesel|Sinema|Ulusal|Global|Diğer|Aktüalite|Spor Programı)\s*[-–]?\s*$",
    re.IGNORECASE,
)


EXTRACT_JS = r"""(channelOverrides) => {
  const timeRe = /\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}/;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();

  const getCode = href => {
    if (!href) return '';
    let decoded = href;
    try { decoded = decodeURIComponent(href); } catch (_) {}
    const match = decoded.match(/\b(ch\d{6,})\b/i);
    return match ? match[1].toLowerCase() : '';
  };

  // Kanal kartlarındaki logo/ad bilgisini aynı kartın bağlantısındaki
  // Tivibu kanal koduyla eşleştir. DOM sırası kullanılmaz.
  const namesByCode = new Map();
  const conflicts = new Map();

  for (const img of document.querySelectorAll('img[alt]')) {
    const alt = norm(img.getAttribute('alt'));
    if (!/^Kanal\s*:/i.test(alt)) continue;

    const name = norm(alt.replace(/^Kanal\s*:\s*/i, ''));
    if (!name) continue;

    let node = img;
    let foundCodes = new Set();

    for (let depth = 0; depth < 7 && node; depth++, node = node.parentElement) {
      const links = [...node.querySelectorAll('a[href]')];
      const codes = new Set(
        links.map(a => getCode(a.href)).filter(Boolean)
      );

      if (codes.size === 1) {
        foundCodes = codes;
        break;
      }
    }

    if (foundCodes.size !== 1) continue;

    const code = [...foundCodes][0];
    if (channelOverrides && channelOverrides[code]) continue;

    if (!namesByCode.has(code)) {
      namesByCode.set(code, name);
    } else if (namesByCode.get(code) !== name) {
      conflicts.set(code, new Set([
        ...(conflicts.get(code) || [namesByCode.get(code)]),
        name
      ]));
    }
  }

  // Programları URL'deki kanal koduna göre grupla.
  const groups = new Map();

  for (const a of document.querySelectorAll('a[href]')) {
    const text = norm(a.innerText || a.textContent);
    if (!a.href || !timeRe.test(text)) continue;

    const code = getCode(a.href);
    if (!code) continue;

    if (!groups.has(code)) groups.set(code, []);
    groups.get(code).push(text);
  }

  const out = [];
  const unresolved = [];
  const conflicting = [];

  for (const [code, items] of groups.entries()) {
    const name = (
      channelOverrides && channelOverrides[code]
    )
      ? channelOverrides[code]
      : namesByCode.get(code) || '';

    if (conflicts.has(code)) {
      conflicting.push({
        code,
        names: [...conflicts.get(code)]
      });
      continue;
    }

    if (!name) {
      unresolved.push({
        code,
        programCount: items.length
      });
      continue;
    }

    out.push({
      name,
      code,
      items
    });
  }

  return {
    rows: out,
    groupCount: groups.size,
    diagnostics: {
      programGroupCount: groups.size,
      mappedGroupCount: out.length,
      unresolved,
      conflicting
    }
  };
}"""


def clean_text(value):
    value = html.unescape(
        str(value or "")
    ).replace("\xa0", " ")

    return re.sub(r"\s+", " ", value).strip()


def slugify(value):
    value = clean_text(value)

    replacements = {
        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
        "â": "a",
        "î": "i",
        "û": "u",
    }

    for old, new in replacements.items():
        value = value.replace(
            old,
            new
        ).replace(
            old.upper(),
            new
        )

    value = unicodedata.normalize(
        "NFKD",
        value
    )

    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    ).lower()

    value = re.sub(
        r"[^a-z0-9]+",
        "_",
        value
    ).strip("_")

    return value or "channel"


def parse_program_line(line):
    line = clean_text(line)
    match = TIME_RE.search(line)

    if not match:
        return None

    title = clean_text(
        line[:match.start()]
    )

    title = re.sub(
        r"\s*[-–]\s*$",
        "",
        title
    )

    title = CATEGORY_RE.sub(
        "",
        title
    ).strip(" -–")

    if not title:
        return None

    sh, sm, eh, em = (
        int(match.group(k))
        for k in ("sh", "sm", "eh", "em")
    )

    if (
        sh > 23
        or eh > 23
        or sm > 59
        or em > 59
    ):
        return None

    return {
        "title": title,
        "sh": sh,
        "sm": sm,
        "eh": eh,
        "em": em,
    }


def date_label(day, today):
    if day == today:
        return "Bugün"

    if day == today + timedelta(days=1):
        return "Yarın"

    return day.strftime("%d.%m.%Y")


def open_category(page, category_name, path):
    url = urljoin(
        BASE_URL,
        path
    )

    print(f"\nKategori açılıyor: {category_name}")
    print(f"Adres: {url}")

    page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT
    )

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=12000
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(1000)


def select_date(page, label):
    locator = page.get_by_text(
        label,
        exact=True
    )

    if locator.count() == 0:
        raise RuntimeError(
            f"Tarih seçeneği bulunamadı: {label}"
        )

    locator.first.click(
        timeout=15000,
        force=True
    )

    page.wait_for_timeout(DATE_WAIT_MS)


def assign_times(items, selected_day, channel_name=""):
    """
    Seçilen Tivibu gününü gerçek takvim tarihine dönüştürür.

    Eski sürüm, ilk yayın 18:00 sonrasında başlıyor ve listede gece
    yarısı geçişi bulunuyorsa tarihi bir gün geri alıyordu. Bu nedenle
    bugünün yayınları XML'de dünkü tarihe yazılabiliyordu.

    Yeni sürümde başlangıç tarihi daima selected_day'dir. Saat gece
    yarısını geçtiğinde yalnızca sonraki programlar ertesi güne alınır.
    """
    parsed = [
        p for p in (
            parse_program_line(x)
            for x in items
        )
        if p
    ]

    if not parsed:
        return []

    current_day = selected_day
    previous_minutes = None
    result = []

    for p in parsed:
        start_minutes = (
            p["sh"] * 60 +
            p["sm"]
        )

        end_minutes = (
            p["eh"] * 60 +
            p["em"]
        )

        if (
            previous_minutes is not None
            and start_minutes < previous_minutes
        ):
            current_day += timedelta(days=1)

        start = datetime(
            current_day.year,
            current_day.month,
            current_day.day,
            p["sh"],
            p["sm"],
            tzinfo=ISTANBUL
        )

        end_day = (
            current_day + timedelta(days=1)
            if end_minutes <= start_minutes
            else current_day
        )

        stop = datetime(
            end_day.year,
            end_day.month,
            end_day.day,
            p["eh"],
            p["em"],
            tzinfo=ISTANBUL
        )

        duration = (
            stop - start
        ).total_seconds()

        if 0 < duration <= 24 * 3600:
            result.append({
                "title": p["title"],
                "start": start,
                "stop": stop
            })

        previous_minutes = start_minutes

    return result


def collect_all(page, today):
    # Kanal adı değil, Tivibu kanal kodu birincil anahtardır.
    # Aynı görünen ada sahip farklı kodlar ayrı kanallar olarak korunur.
    all_channels = {}
    all_programs = {}

    window_start = datetime(
        today.year,
        today.month,
        today.day,
        0,
        0,
        tzinfo=ISTANBUL
    )

    end_day = today + timedelta(days=DAYS)

    window_end = datetime(
        end_day.year,
        end_day.month,
        end_day.day,
        0,
        0,
        tzinfo=ISTANBUL
    )

    for category_name, path in CATEGORY_PAGES:
        open_category(
            page,
            category_name,
            path
        )

        category_channels = set()

        for offset in range(DAYS):
            selected_day = today + timedelta(days=offset)
            label = date_label(
                selected_day,
                today
            )

            print(f"  {label} taranıyor...")

            select_date(
                page,
                label
            )

            extracted = page.evaluate(
                EXTRACT_JS,
                CHANNEL_NAME_OVERRIDES
            )

            rows = extracted.get(
                "rows",
                []
            )

            diagnostics = extracted.get(
                "diagnostics",
                {}
            )

            if diagnostics.get("unresolved"):
                print(
                    f"  EŞLEŞMEYEN KANALLAR "
                    f"({len(diagnostics['unresolved'])}): "
                    f"{diagnostics['unresolved']}"
                )

            if diagnostics.get("conflicting"):
                raise RuntimeError(
                    f"{category_name} / {label}: aynı kanal koduna "
                    f"birden fazla kanal adı bağlandı: "
                    f"{diagnostics['conflicting']}"
                )

            if not rows:
                print(
                    f"  UYARI: {category_name} / {label}: "
                    "doğrulanmış kanal/program eşleşmesi bulunamadı."
                )

                print(
                    "  Tanılama:",
                    diagnostics
                )

                continue

            day_program_count = 0

            for row in rows:
                channel_name = clean_text(
                    row.get("name")
                )

                channel_code = clean_text(
                    row.get("code")
                ).lower()

                if (
                    not channel_name
                    or channel_name.upper() in BAD_CHANNELS
                ):
                    continue

                if not re.fullmatch(
                    r"ch\d{6,}",
                    channel_code
                ):
                    print(
                        f"  UYARI: {channel_name} için geçerli "
                        f"Tivibu kanal kodu bulunamadı; kayıt atlandı. "
                        f"Gelen kod: {channel_code!r}"
                    )

                    continue

                channel_name = CHANNEL_NAME_OVERRIDES.get(
                    channel_code,
                    channel_name
                )

                programs = assign_times(
                    row.get("items", []),
                    selected_day,
                    channel_name
                )

                if not programs:
                    continue

                previous_name = all_channels.get(
                    channel_code
                )

                if (
                    previous_name
                    and previous_name != channel_name
                ):
                    raise RuntimeError(
                        f"KANAL EŞLEŞME HATASI: {channel_code} kodu "
                        f"aynı çalışmada hem {previous_name!r} hem "
                        f"{channel_name!r} adıyla bulundu. "
                        "Güvenilir olmayan EPG üretilmemesi için "
                        "işlem durduruldu."
                    )

                all_channels[channel_code] = channel_name
                category_channels.add(channel_code)

                for program in programs:
                    if (
                        program["stop"] <= window_start
                        or program["start"] >= window_end
                    ):
                        continue

                    key = (
                        channel_code,
                        program["title"].casefold(),
                        program["start"],
                        program["stop"],
                    )

                    if key not in all_programs:
                        all_programs[key] = {
                            "channel_code": channel_code,
                            "channel_name": channel_name,
                            **program,
                        }

                        day_program_count += 1

            print(
                f"    {len(rows)} kart / "
                f"{len(category_channels)} kategori kanalı / "
                f"{day_program_count} yeni program"
            )

        print(
            f"  {category_name}: "
            f"{len(category_channels)} kanal bulundu."
        )

    # Aynı isimli farklı Tivibu ID'leri ayrı kanallar olarak korunur.
    # Oynatıcıda iki Tarih TV kartı varsa iki ID de XML'de kalır.
    names_to_codes = {}

    for code, name in all_channels.items():
        names_to_codes.setdefault(
            name.casefold(),
            []
        ).append(code)

    for name_key, codes in names_to_codes.items():
        if len(codes) > 1:
            display_name = all_channels[codes[0]]

            print(
                f"UYARI: {display_name} adı birden fazla Tivibu ID "
                f"ile bulundu: {', '.join(sorted(codes))}. "
                "Kodlar ayrı ayrı korunacak."
            )

    return all_channels, list(all_programs.values())


def build_xml(channels, programs):
    for channel_code, channel_name in channels.items():
        if not re.fullmatch(
            r"ch\d{6,}",
            clean_text(channel_code).lower()
        ):
            raise RuntimeError(
                f"{channel_name} kanalı için geçerli Tivibu ID "
                f"bulunamadı: {channel_code!r}. XML oluşturulmadı."
            )

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL,
        }
    )

    for channel_code, channel_name in sorted(
        channels.items(),
        key=lambda item: (
            item[1].casefold(),
            item[0]
        )
    ):
        channel = ET.SubElement(
            root,
            "channel",
            {"id": channel_code}
        )

        display = ET.SubElement(
            channel,
            "display-name",
            {"lang": "tr"}
        )

        display.text = channel_name

    for item in sorted(
        programs,
        key=lambda x: (
            x["start"],
            x["channel_code"],
            x["stop"]
        )
    ):
        programme = ET.SubElement(
            root,
            "programme",
            {
                "channel": item["channel_code"],
                "start": item["start"].strftime(
                    "%Y%m%d%H%M%S %z"
                ),
                "stop": item["stop"].strftime(
                    "%Y%m%d%H%M%S %z"
                ),
            }
        )

        title = ET.SubElement(
            programme,
            "title",
            {"lang": "tr"}
        )

        title.text = item["title"]

    tree = ET.ElementTree(root)

    ET.indent(
        tree,
        space="  "
    )

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True
    )


def validate_data(channels, programs, today):
    missing_codes = [
        code for code in channels
        if not re.fullmatch(
            r"ch\d{6,}",
            clean_text(code).lower()
        )
    ]

    if missing_codes:
        sample = ", ".join(
            missing_codes[:10]
        )

        raise RuntimeError(
            f"{len(missing_codes)} kanalın Tivibu ID'si "
            f"eksik/geçersiz: {sample}. Mevcut XML korunuyor."
        )

    if len(channels) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(channels)} kanal bulundu. "
            f"En az {MIN_CHANNELS} kanal bekleniyordu. "
            "Mevcut XML korunuyor."
        )

    if not programs:
        raise RuntimeError(
            "Program bulunamadı. Mevcut XML korunuyor."
        )

    # Seçilen 7 günlük aralığın dışındaki yayınlar XML'e giremez.
    first_day = today
    last_day_exclusive = today + timedelta(days=DAYS)

    out_of_range = [
        p for p in programs
        if not (
            first_day <= p["start"].date() < last_day_exclusive
        )
    ]

    if out_of_range:
        raise RuntimeError(
            "EPG'de seçilen 7 günlük aralığın dışında program bulundu. "
            f"Örnekler: {out_of_range[:5]}. Mevcut XML korunuyor."
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
    print("TIVIBU 7 GÜNLÜK EPG - TARİH DÜZELTMELİ SÜRÜM")
    print(f"Kaynak: {LIVE_TV_URL}")
    print(f"Başlangıç: {today.strftime('%d.%m.%Y')}")
    print(f"Kategori sayısı: {len(CATEGORY_PAGES)}")
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

        page.set_default_timeout(
            PAGE_TIMEOUT
        )

        channels, programs = collect_all(
            page,
            today
        )

        browser.close()

    validate_data(
        channels,
        programs,
        today
    )

    build_xml(
        channels,
        programs
    )

    days = sorted({
        p["start"].date()
        for p in programs
    })

    print("\n" + "=" * 68)
    print("7 GÜNLÜK EPG OLUŞTURULDU")
    print(f"Toplam kanal: {len(channels)}")
    print(f"Toplam program: {len(programs)}")
    print(f"Program tarihi sayısı: {len(days)}")
    print(f"İlk tarih: {days[0]}")
    print(f"Son tarih: {days[-1]}")
    print(f"Dosya: {OUTPUT_FILE}")
    print(f"Süre: {time.time() - started:.1f} saniye")
    print("=" * 68)


if __name__ == "__main__":
    main()
