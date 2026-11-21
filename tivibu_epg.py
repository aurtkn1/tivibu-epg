
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

# Tivibu'nun bütün kategori sayfaları
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

TIME_RE = re.compile(
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})\s*(?:→|➜|[-–])\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})"
)

CATEGORY_RE = re.compile(
    r"\s+(?:Yaşam|Film|Dizi|Spor|Haber|Çocuk|Müzik|Belgesel|Sinema|Ulusal|Global|Diğer|Aktüalite|Spor Programı)\s*[-–]?\s*$",
    re.IGNORECASE,
)


EXTRACT_JS = r"""() => {
  const timeRe = /\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}/;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const anchors = [...document.querySelectorAll('a')];
  const groups = new Map();
  const unmapped = [];

  // Eski çalışan betikteki yöntem korunur:
  // Kanal adı /kanallar/ bağlantısının kendi metninden alınır.
  // Program kartındaki resim alt metninden kanal adı tahmin edilmez.
  for (const programAnchor of anchors) {
    const programText = norm(programAnchor.innerText || programAnchor.textContent);
    if (!programAnchor.href || !timeRe.test(programText)) continue;

    let node = programAnchor;
    let channelHref = '';
    let channelName = '';

    // Program bağlantısının üst elemanlarında en yakın tek kanal
    // bağlantısını ara. Birden fazla kanal içeren üst kapsayıcıları kullanma.
    for (let depth = 0; depth < 35 && node; depth++, node = node.parentElement) {
      const channelLinks = [...node.querySelectorAll('a[href]')]
        .filter(a => {
          try {
            const u = new URL(a.href, location.href);
            return u.pathname.toLowerCase().includes('/kanallar/');
          } catch (_) {
            return false;
          }
        });

      const unique = new Map();
      for (const link of channelLinks) {
        const href = link.href.split('#')[0];
        const name = norm(link.innerText || link.textContent) ||
          norm(link.querySelector('img[alt]')?.getAttribute('alt'))
            .replace(/^Kanal\s*:\s*/i, '');
        if (href && name) unique.set(href, { href, name });
      }

      if (unique.size === 1) {
        const match = [...unique.values()][0];
        channelHref = match.href;
        channelName = match.name;
        break;
      }
    }

    if (!channelHref || !channelName) {
      unmapped.push({
        href: programAnchor.href,
        firstProgram: programText,
        reason: 'Üst kartta tek bir /kanallar/ bağlantısı bulunamadı'
      });
      continue;
    }

    if (!groups.has(channelHref)) {
      groups.set(channelHref, {
        name: channelName,
        items: [],
        seen: new Set()
      });
    }

    const group = groups.get(channelHref);
    if (!group.seen.has(programText)) {
      group.seen.add(programText);
      group.items.push(programText);
    }
  }

  const rows = [];
  for (const [href, group] of groups.entries()) {
    if (group.items.length) {
      rows.push({
        name: group.name,
        href,
        items: group.items
      });
    }
  }

  return {
    rows,
    groupCount: groups.size + unmapped.length,
    diagnostics: {
      programGroupCount: groups.size + unmapped.length,
      mappedGroupCount: rows.length,
      unmappedGroupCount: unmapped.length,
      unmappedSamples: unmapped.slice(0, 10)
    }
  };
}"""


def clean_text(value):
    value = html.unescape(str(value or "")).replace("\xa0", " ")
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
        value = value.replace(old, new).replace(old.upper(), new)

    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        c for c in value if not unicodedata.combining(c)
    ).lower()

    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")

    return value or "channel"


def parse_program_line(line):
    line = clean_text(line)
    match = TIME_RE.search(line)

    if not match:
        return None

    title = clean_text(line[:match.start()])
    title = re.sub(r"\s*[-–]\s*$", "", title)
    title = CATEGORY_RE.sub("", title).strip(" -–")

    if not title:
        return None

    sh, sm, eh, em = (
        int(match.group(k))
        for k in ("sh", "sm", "eh", "em")
    )

    if sh > 23 or eh > 23 or sm > 59 or em > 59:
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
    url = urljoin(BASE_URL, path)

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
    locator = page.get_by_text(label, exact=True)

    if locator.count() == 0:
        raise RuntimeError(
            f"Tarih seçeneği bulunamadı: {label}"
        )

    locator.first.click(
        timeout=15000,
        force=True
    )

    page.wait_for_timeout(DATE_WAIT_MS)


def assign_times(items, selected_day):
    parsed = [
        p for p in (
            parse_program_line(x)
            for x in items
        )
        if p
    ]

    if not parsed:
        return []

    first_minutes = (
        parsed[0]["sh"] * 60 +
        parsed[0]["sm"]
    )

    has_rollover = any(
        (
            parsed[i]["sh"] * 60 +
            parsed[i]["sm"]
        ) < (
            parsed[i - 1]["sh"] * 60 +
            parsed[i - 1]["sm"]
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

    for p in parsed:
        start_minutes = (
            p["sh"] * 60 +
            p["sm"]
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
            if (
                p["eh"] * 60 + p["em"]
            ) <= start_minutes
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

        if (
            stop > start
            and (stop - start).total_seconds() <= 24 * 3600
        ):
            result.append({
                "title": p["title"],
                "start": start,
                "stop": stop
            })

        previous_minutes = start_minutes

    return result


def collect_all(page, today):
    all_channels = set()
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
            label = date_label(selected_day, today)

            print(f"  {label} taranıyor...")

            select_date(page, label)

            extracted = page.evaluate(EXTRACT_JS)
            rows = extracted.get("rows", [])
            diagnostics = extracted.get("diagnostics", {})

            if diagnostics.get("unmappedGroupCount", 0):
                print(
                    f"  EŞLEŞMEYEN PROGRAM GRUBU: "
                    f"{diagnostics.get('unmappedGroupCount')} / "
                    f"{diagnostics.get('programGroupCount')}"
                )
                for sample in diagnostics.get("unmappedSamples", [])[:3]:
                    print(
                        "    Eşleşmedi:",
                        sample.get("firstProgram", "")
                    )

            if not rows:
                print(
                    f"  UYARI: {category_name} / {label}: "
                    f"program grubu bulunamadı."
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

                if (
                    not channel_name
                    or channel_name.upper() in BAD_CHANNELS
                ):
                    continue

                programs = assign_times(
                    row.get("items", []),
                    selected_day
                )

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
    ET.indent(tree, space="  ")

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True
    )


def validate_data(channel_names, programs):
    if len(channel_names) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(channel_names)} kanal bulundu. "
            f"En az {MIN_CHANNELS} kanal bekleniyordu. "
            "Mevcut XML korunuyor."
        )

    if not programs:
        raise RuntimeError(
            "Program bulunamadı. Mevcut XML korunuyor."
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
    print("TIVIBU 7 GÜNLÜK EPG - TÜM KATEGORİLER")
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
