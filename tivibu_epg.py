import html
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"
ISTANBUL = ZoneInfo("Europe/Istanbul")

DAYS = 7
PAGE_TIMEOUT = 60000
DATE_WAIT_MS = 1500

MIN_CHANNELS = 30
MIN_PROGRAMS = 100
MIN_DAYS = 6

CATEGORY_PAGES = [
    ("Tüm Kanallar", "/canli-tv"),
    ("Ulusal", "/canli-tv/ulusal"),
    ("Haber", "/canli-tv/haber"),
    ("Spor", "/canli-tv/spor"),
    ("Sinema", "/canli-tv/sinema"),
    ("Dizi", "/canli-tv/dizi"),
    ("Çocuk", "/canli-tv/cocuk"),
    ("Belgesel", "/canli-tv/belgesel"),
    ("Yaşam-Stil", "/canli-tv/yasam-stil"),
    ("Müzik", "/canli-tv/muzik"),
    ("Global", "/canli-tv/global"),
    ("Diğer", "/canli-tv/diger"),
]

BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
    "TİVİBU TANITIM SAYFASI",
    "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
    "TİVİBU NEDİR?",
}

TIME_RE = re.compile(
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})\s*(?:→|➜|[-–])\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})"
)

CATEGORY_RE = re.compile(
    r"\s+(Yaşam|Film|Dizi|Spor|Haber|Çocuk|Müzik|Belgesel|Sinema|"
    r"Ulusal|Global|Diğer|Aktüalite|Spor Programı)\s*$",
    re.IGNORECASE,
)

EXTRACT_JS = r"""() => {
  const timeRe = /\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}/;
  const norm = value => (value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const rvAnchors = [...document.querySelectorAll('a[href*="/rv?"]')];
  const byCode = new Map();

  const diagnostics = {
    rvLinks: rvAnchors.length,
    mapped: 0,
    unmapped: 0,
    noChannelLabel: 0,
    noPrograms: 0,
    samples: []
  };

  function getProgramLines(container) {
    const elements = [container, ...container.querySelectorAll('*')];
    const lines = [];
    const seen = new Set();

    for (const element of elements) {
      const raw = element.innerText || element.textContent || '';
      if (!timeRe.test(raw)) continue;

      const parts = raw.split(/\n+/).map(norm).filter(Boolean);

      for (const part of parts) {
        if (!timeRe.test(part) || seen.has(part)) continue;
        seen.add(part);
        lines.push(part);
      }
    }

    return lines;
  }

  function getChannelLabels(container) {
    const labels = new Set();

    for (const img of container.querySelectorAll('img[alt]')) {
      const alt = norm(img.getAttribute('alt'));
      const match = alt.match(/^Kanal\s*:\s*(.+)$/i);

      if (match && norm(match[1])) {
        labels.add(norm(match[1]));
      }
    }

    return [...labels];
  }

  for (const anchor of rvAnchors) {
    const href = anchor.getAttribute('href') || '';
    let decoded = href;

    try {
      decoded = decodeURIComponent(href);
    } catch (_) {}

    const codeMatch = decoded.match(/\b(ch\d{6,})\b/i);
    if (!codeMatch) continue;

    const channelCode = codeMatch[1].toLowerCase();

    let node = anchor;
    let chosenName = '';
    let chosenItems = [];
    let chosenDepth = 0;

    for (let depth = 0; depth < 22 && node; depth++, node = node.parentElement) {
      const labels = getChannelLabels(node);

      if (labels.length !== 1) continue;

      const items = getProgramLines(node);
      if (!items.length) continue;

      chosenName = labels[0];
      chosenItems = items;
      chosenDepth = depth;
      break;
    }

    if (!chosenName) {
      diagnostics.noChannelLabel++;
      diagnostics.unmapped++;

      if (diagnostics.samples.length < 12) {
        diagnostics.samples.push({
          code: channelCode,
          href,
          reason: 'Tek bir Kanal: görsel etiketi bulunamadı'
        });
      }

      continue;
    }

    if (!chosenItems.length) {
      diagnostics.noPrograms++;
      diagnostics.unmapped++;
      continue;
    }

    const previous = byCode.get(channelCode);

    if (!previous || chosenItems.length > previous.items.length) {
      byCode.set(channelCode, {
        code: channelCode,
        name: chosenName,
        items: chosenItems,
        depth: chosenDepth,
        href
      });
    }
  }

  const rows = [...byCode.values()];

  diagnostics.mapped = rows.length;
  diagnostics.unmapped = Math.max(
    diagnostics.unmapped,
    rvAnchors.length - rows.length
  );

  return {rows, diagnostics};
}"""


def clean_text(value):
    value = html.unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_name(value):
    return clean_text(value).upper()


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
        char for char in value
        if not unicodedata.combining(char)
    ).lower()

    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")

    return value or "channel"


def parse_program_line(line):
    line = clean_text(line)
    match = TIME_RE.search(line)

    if not match:
        return None

    before_time = clean_text(line[:match.start()]).strip(" -–")
    category = ""

    category_match = CATEGORY_RE.search(before_time)

    if category_match:
        category = category_match.group(1)
        before_time = clean_text(
            before_time[:category_match.start()]
        ).strip(" -–")

    title = re.sub(
        r"\s+Canlı\s*$",
        "",
        before_time,
        flags=re.IGNORECASE
    ).strip(" -–")

    if not title:
        return None

    sh, sm, eh, em = (
        int(match.group(key))
        for key in ("sh", "sm", "eh", "em")
    )

    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        return None

    return {
        "title": title,
        "category": category,
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


def select_date(page, selected_day):
    date_prefix = selected_day.strftime("%Y.%m.%d")

    result = page.evaluate(
        """(datePrefix) => {
            const links = [
                ...document.querySelectorAll(
                    'a[channeldatebegin][channeldateend]'
                )
            ];

            const link = links.find(a => {
                const value = a.getAttribute('channeldatebegin') || '';
                return value.startsWith(datePrefix);
            });

            if (!link) {
                return {
                    success: false,
                    available: links.map(a =>
                        a.getAttribute('channeldatebegin')
                    )
                };
            }

            link.click();

            return {
                success: true,
                date: link.getAttribute('channeldatebegin')
            };
        }""",
        date_prefix
    )

    if not result.get("success"):
        raise RuntimeError(
            f"Tarih seçeneği bulunamadı: {date_prefix} "
            f"| Mevcut tarihler: {result.get('available', [])}"
        )

    page.wait_for_timeout(DATE_WAIT_MS)

    active = page.evaluate(
        """(datePrefix) => {
            const links = [
                ...document.querySelectorAll(
                    'a[channeldatebegin][channeldateend]'
                )
            ];

            const link = links.find(a =>
                (a.getAttribute('channeldatebegin') || '')
                    .startsWith(datePrefix)
            );

            return !!link &&
                link.classList.contains('active');
        }""",
        date_prefix
    )

    if not active:
        raise RuntimeError(
            f"Tarih tıklandı ancak aktif olmadı: {date_prefix}"
        )


def assign_times(items, selected_day):
    parsed = [
        item
        for item in (
            parse_program_line(line)
            for line in items
        )
        if item
    ]

    if not parsed:
        return []

    current_day = selected_day
    previous_start_minutes = None
    result = []

    for item in parsed:
        start_minutes = item["sh"] * 60 + item["sm"]
        end_minutes = item["eh"] * 60 + item["em"]

        if (
            previous_start_minutes is not None
            and start_minutes < previous_start_minutes
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

        end_day = (
            current_day + timedelta(days=1)
            if end_minutes <= start_minutes
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
                "category": item["category"],
                "start": start,
                "stop": stop,
            })

        previous_start_minutes = start_minutes

    return result


def wait_page(page):
    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=12000
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(1800)


def collect_all(page, today):
    channels = {}
    programs = {}

    window_start = datetime(
        today.year,
        today.month,
        today.day,
        0,
        0,
        tzinfo=ISTANBUL
    )

    last_day = today + timedelta(days=DAYS)

    window_end = datetime(
        last_day.year,
        last_day.month,
        last_day.day,
        0,
        0,
        tzinfo=ISTANBUL
    )

    for category_name, category_path in CATEGORY_PAGES:
        category_url = BASE_URL + category_path

        print(
            f"\nKATEGORİ: {category_name}",
            flush=True
        )

        page.goto(
            category_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        wait_page(page)

        category_channels = set()

        for offset in range(DAYS):
            selected_day = today + timedelta(days=offset)
            label = date_label(selected_day, today)

            print(
                f"  [{offset + 1}/{DAYS}] {label} taranıyor...",
                flush=True
            )

            select_date(page, selected_day)

            extracted = page.evaluate(EXTRACT_JS)

            rows = extracted.get("rows", [])
            diagnostics = extracted.get("diagnostics", {})
            day_programs = 0

            for row in rows:
                code = clean_text(row.get("code")).lower()
                name = clean_text(row.get("name"))

                if (
                    not code
                    or not name
                    or normalize_name(name) in BAD_CHANNELS
                ):
                    continue

                if code not in channels:
                    channels[code] = {"name": name}
                elif len(name) > len(channels[code]["name"]):
                    channels[code]["name"] = name

                channel_name = channels[code]["name"]
                category_channels.add(code)

                parsed_programs = assign_times(
                    row.get("items", []),
                    selected_day
                )

                for program in parsed_programs:
                    if (
                        program["stop"] <= window_start
                        or program["start"] >= window_end
                    ):
                        continue

                    key = (
                        code,
                        program["start"],
                        program["stop"],
                        program["title"].casefold(),
                    )

                    if key not in programs:
                        programs[key] = {
                            "channel_code": code,
                            "channel_name": channel_name,
                            **program,
                        }

                        day_programs += 1

            print(
                f"    {len(rows)} eşleşen kanal grubu, "
                f"{day_programs} yeni program | "
                f"rv bağlantısı: {diagnostics.get('rvLinks', 0)}, "
                f"eşleşmeyen: {diagnostics.get('unmapped', 0)}"
            )

            if diagnostics.get("samples") and offset == 0:
                for sample in diagnostics["samples"][:4]:
                    print(
                        f"      Eşleşmedi: {sample.get('code')} | "
                        f"{sample.get('reason')}"
                    )

        print(
            f"  {category_name}: "
            f"{len(category_channels)} kanal bulundu."
        )

    return channels, list(programs.values())


def build_xml(channels, programs):
    used_ids = set()
    channel_ids = {}

    for code, info in sorted(
        channels.items(),
        key=lambda item: item[1]["name"].casefold()
    ):
        base = slugify(info["name"])
        channel_id = base
        suffix = 2

        while channel_id in used_ids:
            channel_id = f"{base}_{suffix}"
            suffix += 1

        used_ids.add(channel_id)
        channel_ids[code] = channel_id

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL,
        }
    )

    for code, info in sorted(
        channels.items(),
        key=lambda item: item[1]["name"].casefold()
    ):
        node = ET.SubElement(
            root,
            "channel",
            {"id": channel_ids[code]}
        )

        display = ET.SubElement(
            node,
            "display-name",
            {"lang": "tr"}
        )

        display.text = info["name"]

    for item in sorted(
        programs,
        key=lambda value: (
            value["start"],
            value["channel_code"],
            value["stop"]
        )
    ):
        node = ET.SubElement(
            root,
            "programme",
            {
                "channel": channel_ids[item["channel_code"]],
                "start": item["start"].strftime("%Y%m%d%H%M%S %z"),
                "stop": item["stop"].strftime("%Y%m%d%H%M%S %z"),
            }
        )

        title = ET.SubElement(
            node,
            "title",
            {"lang": "tr"}
        )

        title.text = item["title"]

        if item.get("category"):
            category = ET.SubElement(
                node,
                "category",
                {"lang": "tr"}
            )

            category.text = item["category"]

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    temp_file = OUTPUT_FILE + ".tmp"

    tree.write(
        temp_file,
        encoding="utf-8",
        xml_declaration=True
    )

    os.replace(temp_file, OUTPUT_FILE)


def validate_data(channels, programs):
    if len(channels) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(channels)} kanal eşleşti; "
            f"en az {MIN_CHANNELS} bekleniyordu. "
            "Mevcut epg.xml korunuyor."
        )

    if len(programs) < MIN_PROGRAMS:
        raise RuntimeError(
            f"Yalnızca {len(programs)} program alındı; "
            f"en az {MIN_PROGRAMS} bekleniyordu. "
            "Mevcut epg.xml korunuyor."
        )

    days = {
        program["start"].date()
        for program in programs
    }

    if len(days) < MIN_DAYS:
        raise RuntimeError(
            f"Yalnızca {len(days)} yayın günü bulundu; "
            f"en az {MIN_DAYS} bekleniyordu. "
            "Mevcut epg.xml korunuyor."
        )


def main():
    started = time.time()
    today = datetime.now(ISTANBUL).date()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("Kanal ID'si /rv bağlantısından, kanal adı Kanal: görselinden alınır")
    print(f"Kaynak: {LIVE_TV_URL}")
    print(f"Başlangıç: {today.strftime('%d.%m.%Y')}")
    print(f"Kategori sayısı: {len(CATEGORY_PAGES)}")
    print("=" * 68)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ],
        )

        page = browser.new_page(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1440,
                "height": 1100
            },
        )

        page.set_default_timeout(PAGE_TIMEOUT)

        try:
            channels, programs = collect_all(page, today)
        finally:
            browser.close()

    validate_data(channels, programs)
    build_xml(channels, programs)

    days = sorted({
        program["start"].date()
        for program in programs
    })

    print("\n" + "=" * 68)
    print("7 GÜNLÜK EPG OLUŞTURULDU")
    print(f"Kanal: {len(channels)}")
    print(f"Program: {len(programs)}")
    print(f"Yayın günü: {len(days)}")
    print(f"İlk gün: {days[0].strftime('%d.%m.%Y')}")
    print(f"Son gün: {days[-1].strftime('%d.%m.%Y')}")
    print(f"Dosya: {OUTPUT_FILE}")
    print(f"Süre: {time.time() - started:.1f} saniye")
    print("=" * 68)


if __name__ == "__main__":
    main()
