
import re
import html
import time
import unicodedata
import os
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
DATE_WAIT_MS = 1600
MIN_CHANNELS = 30
MIN_PROGRAMS = 100

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
    r"\s+(?:Yaşam|Film|Dizi|Spor|Haber|Çocuk|Müzik|Belgesel|Sinema|"
    r"Ulusal|Global|Diğer|Aktüalite|Spor Programı)\s*[-–]?\s*$",
    re.IGNORECASE,
)

EXTRACT_JS = r"""() => {
  const timeRe = /\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}/;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const channelAnchors = [...document.querySelectorAll('a[href*="/kanallar/"]')];
  const rows = [];
  const seenChannels = new Set();
  const diagnostics = {
    channelAnchors: channelAnchors.length,
    mapped: 0,
    unmapped: 0,
    timeElements: 0,
    samples: []
  };

  const allTimeElements = [...document.querySelectorAll('body *')].filter(el => {
    const text = norm(el.innerText || el.textContent);
    return text && timeRe.test(text);
  });
  diagnostics.timeElements = allTimeElements.length;

  function getProgramElements(container) {
    const candidates = [...container.querySelectorAll('*')].filter(el => {
      const text = norm(el.innerText || el.textContent);
      return text && timeRe.test(text);
    });

    const leaves = candidates.filter(el => {
      return ![...el.children].some(child => {
        const childText = norm(child.innerText || child.textContent);
        return childText && timeRe.test(childText);
      });
    });

    const result = [];
    const seen = new Set();
    for (const el of leaves) {
      const text = norm(el.innerText || el.textContent);
      if (!text || !timeRe.test(text) || seen.has(text)) continue;
      seen.add(text);
      result.push(text);
    }
    return result;
  }

  for (const channelAnchor of channelAnchors) {
    let channelHref = '';
    try {
      const u = new URL(channelAnchor.href, location.href);
      if (!u.pathname.toLowerCase().includes('/kanallar/')) continue;
      channelHref = u.origin + u.pathname;
    } catch (_) { continue; }

    if (!channelHref || seenChannels.has(channelHref)) continue;

    let channelName = norm(channelAnchor.innerText || channelAnchor.textContent);
    if (!channelName) {
      const img = channelAnchor.querySelector('img[alt]');
      channelName = norm(img ? img.getAttribute('alt') : '');
    }
    channelName = channelName.replace(/^Kanal\s*:\s*/i, '').trim();
    if (!channelName) continue;

    let node = channelAnchor.parentElement;
    let chosenItems = [];
    let chosenDepth = 0;

    for (let depth = 1; depth <= 24 && node; depth++, node = node.parentElement) {
      const channelLinks = [...node.querySelectorAll('a[href*="/kanallar/"]')];
      const uniqueChannelHrefs = new Set();
      for (const link of channelLinks) {
        try {
          const u = new URL(link.href, location.href);
          uniqueChannelHrefs.add(u.origin + u.pathname);
        } catch (_) {}
      }

      if (uniqueChannelHrefs.size > 1) continue;

      const items = getProgramElements(node);
      if (items.length) {
        chosenItems = items;
        chosenDepth = depth;
        break;
      }
    }

    if (!chosenItems.length) {
      diagnostics.unmapped++;
      if (diagnostics.samples.length < 12) {
        diagnostics.samples.push({
          name: channelName,
          href: channelHref,
          reason: 'Kanal bağlantısının üst kapsayıcılarında saat içeren program satırı bulunamadı'
        });
      }
      continue;
    }

    seenChannels.add(channelHref);
    rows.push({ name: channelName, href: channelHref, items: chosenItems, depth: chosenDepth });
  }

  diagnostics.mapped = rows.length;
  return { rows, diagnostics };
}"""


def clean_text(value):
    value = html.unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_name(value):
    return clean_text(value).upper()


def is_bad_channel(name):
    name = normalize_name(name)
    return name in BAD_CHANNELS


def slugify(value):
    value = clean_text(value)
    replacements = {
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o",
        "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u",
    }
    for old, new in replacements.items():
        value = value.replace(old, new).replace(old.upper(), new)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value or "channel"


def unique_id(name, used):
    base = slugify(name)
    result = base
    suffix = 2
    while result in used:
        result = f"{base}_{suffix}"
        suffix += 1
    used.add(result)
    return result


def parse_program_line(line):
    line = clean_text(line)
    match = TIME_RE.search(line)
    if not match:
        return None

    title = clean_text(line[:match.start()])
    title = re.sub(r"\s*[-–]\s*$", "", title)
    title = CATEGORY_RE.sub("", title).strip(" -–")
    title = re.sub(
        r"\s+(?:Yaşam|Film|Dizi|Spor|Haber|Çocuk|Müzik|Belgesel|Sinema)\s*$",
        "", title, flags=re.IGNORECASE
    ).strip()

    if not title:
        return None

    sh, sm, eh, em = (
        int(match.group(k))
        for k in ("sh", "sm", "eh", "em")
    )

    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        return None

    return {"title": title, "sh": sh, "sm": sm, "eh": eh, "em": em}


def date_label(day, today):
    if day == today:
        return "Bugün"
    if day == today + timedelta(days=1):
        return "Yarın"
    return day.strftime("%d.%m.%Y")


def select_date(page, label):
    locator = page.get_by_text(label, exact=True)
    count = locator.count()
    if count == 0:
        raise RuntimeError(f"Tivibu sayfasında '{label}' tarih seçeneği bulunamadı.")

    locator.first.click(timeout=15000, force=True)

    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(DATE_WAIT_MS)


def assign_times(items, selected_day):
    parsed = [p for p in (parse_program_line(x) for x in items) if p]
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
        if previous_minutes is not None and start_minutes < previous_minutes:
            current_day += timedelta(days=1)

        start = datetime(
            current_day.year, current_day.month, current_day.day,
            p["sh"], p["sm"], tzinfo=ISTANBUL
        )

        end_day = (
            current_day + timedelta(days=1)
            if (p["eh"] * 60 + p["em"]) <= start_minutes
            else current_day
        )

        stop = datetime(
            end_day.year, end_day.month, end_day.day,
            p["eh"], p["em"], tzinfo=ISTANBUL
        )

        if stop > start and (stop - start).total_seconds() <= 24 * 3600:
            result.append({"title": p["title"], "start": start, "stop": stop})

        previous_minutes = start_minutes

    return result


def collect_seven_days(page, today):
    all_channels = {}
    all_programs = {}
    window_start = datetime(today.year, today.month, today.day, 0, 0, tzinfo=ISTANBUL)
    window_end_day = today + timedelta(days=DAYS)
    window_end = datetime(
        window_end_day.year, window_end_day.month, window_end_day.day,
        0, 0, tzinfo=ISTANBUL
    )

    for offset in range(DAYS):
        selected_day = today + timedelta(days=offset)
        label = date_label(selected_day, today)
        print(f"[{offset + 1}/{DAYS}] {label} yayın akışı alınıyor...", flush=True)

        select_date(page, label)
        extracted = page.evaluate(EXTRACT_JS)
        rows = extracted.get("rows", [])
        diagnostics = extracted.get("diagnostics", {})

        if not rows:
            raise RuntimeError(
                f"{label} için kanal/program kartı okunamadı. "
                f"Kanal bağlantısı: {diagnostics.get('channelAnchors', 0)}, "
                f"eşleşmeyen: {diagnostics.get('unmapped', 0)}. "
                "Mevcut XML korunuyor."
            )

        day_program_count = 0
        day_channel_count = 0

        for row in rows:
            channel_name = clean_text(row.get("name"))
            if not channel_name or is_bad_channel(channel_name):
                continue

            programs = assign_times(row.get("items", []), selected_day)
            if not programs:
                continue

            if channel_name not in all_channels:
                all_channels[channel_name] = channel_name
                day_channel_count += 1

            for program in programs:
                if program["stop"] <= window_start or program["start"] >= window_end:
                    continue

                key = (
                    channel_name.casefold(),
                    program["title"].casefold(),
                    program["start"],
                    program["stop"],
                )

                if key not in all_programs:
                    all_programs[key] = {"channel_name": channel_name, **program}
                    day_program_count += 1

        print(
            f"  {len(rows)} kanal kartı, {day_channel_count} yeni kanal, "
            f"{day_program_count} yeni program"
        )

        if diagnostics.get("unmapped"):
            print(
                f"  Bilgi: {diagnostics['unmapped']} kanal bağlantısı eşleştirilemedi; "
                f"{diagnostics.get('mapped', 0)} kart başarıyla eşleşti. "
                f"Saat içeren DOM elemanı: {diagnostics.get('timeElements', 0)}"
            )
            if diagnostics.get("mapped", 0) < MIN_CHANNELS and offset == 0:
                for sample in diagnostics.get("samples", [])[:5]:
                    print(f"    Eşleşmeyen kanal: {sample.get('name')} | {sample.get('reason')}")

    return sorted(all_channels.values(), key=str.casefold), list(all_programs.values())


def build_xml(channel_names, programs):
    used_ids = set()
    channel_ids = {
        name: unique_id(name, used_ids)
        for name in sorted(channel_names, key=str.casefold)
    }

    root = ET.Element("tv", {
        "generator-info-name": "Tivibu 7 Günlük EPG",
        "generator-info-url": LIVE_TV_URL,
    })

    for name in sorted(channel_names, key=str.casefold):
        channel = ET.SubElement(root, "channel", {"id": channel_ids[name]})
        display = ET.SubElement(channel, "display-name", {"lang": "tr"})
        display.text = name

    for item in sorted(
        programs,
        key=lambda x: (x["start"], x["channel_name"].casefold(), x["stop"])
    ):
        programme = ET.SubElement(root, "programme", {
            "channel": channel_ids[item["channel_name"]],
            "start": item["start"].strftime("%Y%m%d%H%M%S %z"),
            "stop": item["stop"].strftime("%Y%m%d%H%M%S %z"),
        })
        title = ET.SubElement(programme, "title", {"lang": "tr"})
        title.text = item["title"]

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    temp_file = OUTPUT_FILE + ".tmp"
    tree.write(temp_file, encoding="utf-8", xml_declaration=True)
    os.replace(temp_file, OUTPUT_FILE)


def validate_data(channel_names, programs):
    if len(channel_names) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(channel_names)} kanal bulundu. "
            f"En az {MIN_CHANNELS} kanal bekleniyordu. Mevcut XML korunuyor."
        )

    if len(programs) < MIN_PROGRAMS:
        raise RuntimeError(
            f"Yalnızca {len(programs)} program bulundu. "
            f"En az {MIN_PROGRAMS} program bekleniyordu. Mevcut XML korunuyor."
        )

    days = {p["start"].date() for p in programs}
    if len(days) < 6:
        raise RuntimeError(
            f"Yalnızca {len(days)} farklı yayın günü bulundu. Eksik veriyle XML yazılmadı."
        )


def main():
    started = time.time()
    today = datetime.now(ISTANBUL).date()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("Kanal kartlarından doğrudan eşleştirme")
    print(f"Kaynak: {LIVE_TV_URL}")
    print(f"Başlangıç: {today.strftime('%d.%m.%Y')}")
    print("Kategori taraması: Kapalı (Tüm Kanallar sayfası kullanılıyor)")
    print("=" * 68)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        page = browser.new_page(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1440, "height": 1100}
        )
        page.set_default_timeout(PAGE_TIMEOUT)

        page.goto(
            LIVE_TV_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(2500)
        channel_names, programs = collect_seven_days(page, today)
        browser.close()

    validate_data(channel_names, programs)
    build_xml(channel_names, programs)

    days = sorted({p["start"].date() for p in programs})

    print("\n" + "=" * 68)
    print("7 GÜNLÜK EPG OLUŞTURULDU")
    print(f"Toplam kanal: {len(channel_names)}")
    print(f"Toplam program: {len(programs)}")
    print(f"Program tarihi sayısı: {len(days)}")
    print(f"İlk tarih: {days[0].strftime('%d.%m.%Y')}")
    print(f"Son tarih: {days[-1].strftime('%d.%m.%Y')}")
    print(f"Dosya: {OUTPUT_FILE}")
    print(f"Süre: {time.time() - started:.1f} saniye")
    print("=" * 68)


if __name__ == "__main__":
    main()
