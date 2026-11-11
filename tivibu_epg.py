
import re
import html
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


LIVE_TV_URL = "https://www.tivibu.com.tr/canli-tv"
OUTPUT_FILE = "epg.xml"
ISTANBUL = ZoneInfo("Europe/Istanbul")
DAYS = 7
PAGE_TIMEOUT = 60000
DATE_WAIT_MS = 1800

BAD_CHANNELS = {
    "BENİM KANALIM", "FAVORİ KANALLARIM", "TİVİBU TANITIM SAYFASI",
    "TİVİBU CANLI TV, KANAL VE PROGRAMLAR", "TİVİBU NEDİR?",
    "TİVİBU SPOR CANLI İZLE", "TRT1 CANLI İZLE",
}

BAD_CHANNEL_PARTS = {
    "FAVORİ KANALLARIM", "CANLI TV, KANAL VE PROGRAMLAR",
    "TİVİBU SPOR CANLI İZLE", "TRT1 CANLI İZLE",
}

TIME_RE = re.compile(
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})\s*(?:→|➜|[-–])\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})"
)

CATEGORY_RE = re.compile(
    r"\s+(?:Yaşam|Film|Dizi|Spor|Haber|Çocuk|Müzik|Belgesel|Sinema|Ulusal|Global|Diğer)\s*[-–]?\s*$",
    re.IGNORECASE,
)


def clean_text(value):
    value = html.unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_name(value):
    return clean_text(value).upper()


def is_bad_channel(name):
    name = normalize_name(name)
    return name in BAD_CHANNELS or any(
        part in name for part in BAD_CHANNEL_PARTS
    )


def slugify(value):
    value = clean_text(value)

    replacements = {
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o",
        "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"
    }

    for old, new in replacements.items():
        value = value.replace(old, new).replace(old.upper(), new)

    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        c for c in value if not unicodedata.combining(c)
    ).lower()

    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value or "channel"


def unique_id(name, used):
    base = slugify(name)
    result = base
    n = 2

    while result in used:
        result = f"{base}_{n}"
        n += 1

    used.add(result)
    return result


# Tivibu ana sayfasındaki kanal/program eşleştirmesi.
EXTRACT_JS = r"""() => {
  const timeRe = /\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}/;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();

  // Program bağlantılarını href değerlerine göre grupla.
  const groups = new Map();

  for (const a of document.querySelectorAll('a')) {
    const text = norm(a.innerText || a.textContent);

    if (!timeRe.test(text) || !a.href) continue;

    if (!groups.has(a.href)) {
      groups.set(a.href, []);
    }

    groups.get(a.href).push(text);
  }

  // Kanal logosunun alt metninden kanal adını ve yayın bağlantısını al.
  const channelByHref = new Map();

  for (const img of document.querySelectorAll('img[alt]')) {
    const alt = norm(img.getAttribute('alt'));

    if (!/^Kanal\s*:/i.test(alt)) continue;

    const name = alt.replace(/^Kanal\s*:\s*/i, '').trim();
    const a = img.closest('a');

    if (name && a && a.href && groups.has(a.href)) {
      channelByHref.set(a.href, name);
    }
  }

  // Kanal başlıklarını DOM sırasıyla topla.
  const candidates = [];

  for (const img of document.querySelectorAll('img[alt]')) {
    const alt = norm(img.getAttribute('alt'));

    if (!/^Kanal\s*:/i.test(alt)) continue;

    const a = img.closest('a');
    const name = alt.replace(/^Kanal\s*:\s*/i, '').trim();

    if (a && name) {
      candidates.push({node: a, name});
    }
  }

  for (const a of document.querySelectorAll('a[href*="/kanallar/"]')) {
    const name = norm(a.innerText || a.textContent);

    if (name) {
      candidates.push({node: a, name});
    }
  }

  candidates.sort((a, b) => {
    const relation = a.node.compareDocumentPosition(b.node);

    if (relation & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
    if (relation & Node.DOCUMENT_POSITION_PRECEDING) return 1;

    return 0;
  });

  const orderedNames = [];

  for (const item of candidates) {
    if (!orderedNames.includes(item.name)) {
      orderedNames.push(item.name);
    }
  }

  // Programı olmayan kişisel kanal kartını listeden çıkar.
  const noProgramNames = new Set([
    'BENİM KANALIM',
    'FAVORİ KANALLARIM'
  ]);

  const filteredNames = orderedNames.filter(
    name => !noProgramNames.has(name.toLocaleUpperCase('tr-TR'))
  );

  const entries = [...groups.entries()];
  const out = [];

  for (let i = 0; i < entries.length; i++) {
    const [href, items] = entries[i];

    let name = channelByHref.get(href) || filteredNames[i] || '';

    if (!name) {
      const matchingLink = [...document.querySelectorAll('a')]
        .find(a => a.href === href);

      const image = matchingLink &&
        matchingLink.querySelector('img[alt^="Kanal"]');

      if (image) {
        name = norm(image.alt).replace(/^Kanal\s*:\s*/i, '');
      }
    }

    if (name && items.length) {
      out.push({name, href, items});
    }
  }

  return {
    rows: out,
    diagnostics: {
      channelLogoCount:
        document.querySelectorAll('img[alt*="Kanal"]').length,

      channelPageLinkCount:
        document.querySelectorAll('a[href*="/kanallar/"]').length,

      programLinkCount:
        [...document.querySelectorAll('a')].filter(
          a => timeRe.test(norm(a.innerText || a.textContent))
        ).length,

      programGroupCount: groups.size,
      orderedChannelNameCount: orderedNames.length,
      filteredChannelNameCount: filteredNames.length,
      mappedGroupCount: out.length,

      sampleImageAlt:
        [...document.querySelectorAll('img[alt*="Kanal"]')]
          .slice(0, 5).map(i => i.alt),

      sampleChannelLinks:
        [...document.querySelectorAll('a[href*="/kanallar/"]')]
          .slice(0, 5)
          .map(a => norm(a.innerText || a.textContent))
    }
  };
}"""


def parse_program_line(line):
    line = clean_text(line)
    match = TIME_RE.search(line)

    if not match:
        return None

    title = clean_text(line[:match.start()])
    title = re.sub(r"\s*[-–]\s*$", "", title)
    title = CATEGORY_RE.sub("", title).strip(" -–")

    title = re.sub(
        r"\s+(?:Yaşam|Film|Dizi|Spor|Haber|Çocuk|Müzik|Belgesel)\s*$",
        "",
        title,
        flags=re.I
    ).strip()

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
        "em": em
    }


def date_label(day, today):
    if day == today:
        return "Bugün"

    if day == today + timedelta(days=1):
        return "Yarın"

    return day.strftime("%d.%m.%Y")


def click_date(page, label):
    locator = page.get_by_text(label, exact=True)
    count = locator.count()

    if count == 0:
        raise RuntimeError(
            f"Tivibu sayfasında '{label}' tarih seçeneği bulunamadı."
        )

    locator.first.click(timeout=12000, force=True)
    page.wait_for_timeout(DATE_WAIT_MS)


def assign_times(items, selected_day):
    parsed = [
        p for p in (parse_program_line(x) for x in items)
        if p
    ]

    if not parsed:
        return []

    first_minutes = parsed[0]["sh"] * 60 + parsed[0]["sm"]

    has_rollover = any(
        (parsed[i]["sh"] * 60 + parsed[i]["sm"]) <
        (parsed[i - 1]["sh"] * 60 + parsed[i - 1]["sm"])
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
        start_minutes = p["sh"] * 60 + p["sm"]

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
            if (p["eh"] * 60 + p["em"]) <= start_minutes
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


def collect_seven_days(page, today):
    all_channels = {}
    all_programs = {}

    window_start = datetime(
        today.year, today.month, today.day,
        0, 0, tzinfo=ISTANBUL
    )

    window_end_day = today + timedelta(days=DAYS)

    window_end = datetime(
        window_end_day.year,
        window_end_day.month,
        window_end_day.day,
        0, 0,
        tzinfo=ISTANBUL
    )

    for offset in range(DAYS):
        selected_day = today + timedelta(days=offset)
        label = date_label(selected_day, today)

        print(
            f"\n[{offset + 1}/{DAYS}] "
            f"{label} yayın akışı alınıyor..."
        )

        click_date(page, label)

        extracted = page.evaluate(EXTRACT_JS)
        rows = extracted.get("rows", [])

        if not rows:
            print(
                "  DOM tanılama:",
                extracted.get("diagnostics", {})
            )

            raise RuntimeError(
                f"{label} için kanal/program eşleştirmesi yapılamadı. "
                "XML eski verinin üzerine yazılmadı."
            )

        day_count = 0

        for row in rows:
            channel_name = clean_text(row.get("name"))

            if not channel_name or is_bad_channel(channel_name):
                continue

            programs = assign_times(
                row.get("items", []),
                selected_day
            )

            if not programs:
                continue

            all_channels[channel_name] = channel_name

            for p in programs:
                if (
                    p["stop"] <= window_start
                    or p["start"] >= window_end
                ):
                    continue

                key = (
                    channel_name,
                    p["title"].casefold(),
                    p["start"],
                    p["stop"]
                )

                all_programs[key] = {
                    "channel_name": channel_name,
                    **p
                }

                day_count += 1

        print(
            f"  {len(rows)} kanal kartı, "
            f"{day_count} program okundu."
        )

    return (
        list(all_channels.values()),
        list(all_programs.values())
    )


def write_xml(channel_names, programs):
    used_ids = set()

    channel_ids = {
        name: unique_id(name, used_ids)
        for name in sorted(channel_names, key=str.casefold)
    }

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL,
        }
    )

    for name in sorted(channel_names, key=str.casefold):
        ch = ET.SubElement(
            root,
            "channel",
            {"id": channel_ids[name]}
        )

        display = ET.SubElement(
            ch,
            "display-name",
            {"lang": "tr"}
        )

        display.text = name

    for p in sorted(
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
                "channel": channel_ids[p["channel_name"]],
                "start": p["start"].strftime("%Y%m%d%H%M%S %z"),
                "stop": p["stop"].strftime("%Y%m%d%H%M%S %z"),
            }
        )

        title_node = ET.SubElement(
            programme,
            "title",
            {"lang": "tr"}
        )

        title_node.text = p["title"]

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True
    )


def validate_xml(channel_names, programs):
    if not channel_names:
        raise RuntimeError(
            "Hiç kanal bulunamadı; mevcut epg.xml korunuyor."
        )

    if not programs:
        raise RuntimeError(
            "Hiç program bulunamadı; mevcut epg.xml korunuyor."
        )

    days = {
        p["start"].date()
        for p in programs
    }

    if len(days) < 5:
        raise RuntimeError(
            f"Yalnızca {len(days)} farklı yayın günü bulundu. "
            "Eksik veriyle XML yazılmadı."
        )


def main():
    started = time.time()
    now = datetime.now(ISTANBUL)
    today = now.date()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG")
    print(f"Kaynak: {LIVE_TV_URL}")
    print(f"Başlangıç: {today.strftime('%d.%m.%Y')}")
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

        page.goto(
            LIVE_TV_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=15000
            )
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(2500)

        channels, programs = collect_seven_days(
            page,
            today
        )

        browser.close()

    validate_xml(channels, programs)
    write_xml(channels, programs)

    day_count = len({
        p["start"].date()
        for p in programs
    })

    print("\n" + "=" * 68)
    print("7 GÜNLÜK EPG OLUŞTURULDU")
    print(f"Kanal: {len(channels)}")
    print(f"Program: {len(programs)}")
    print(f"Yayın günü: {day_count}")
    print(f"Dosya: {OUTPUT_FILE}")
    print(f"Süre: {time.time() - started:.1f} saniye")
    print("=" * 68)


if __name__ == "__main__":
    main()
