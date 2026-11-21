
import re
import html
import time
import unicodedata
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from xml.etree import ElementTree as ET


BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"

ISTANBUL = ZoneInfo("Europe/Istanbul")
DAYS = 7

PAGE_TIMEOUT = 60000
DATE_WAIT_MS = 2500
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


# JavaScript yalnızca izole edilmiş, statik HTML sayfasında çalıştırılır.
# Canlı Tivibu sayfasında evaluate/evaluate_all kullanılmaz.
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

  function getProgramElements(container) {
    const candidates = [...container.querySelectorAll('a')].filter(el => {
      const text = norm(el.innerText || el.textContent);
      return text && timeRe.test(text);
    });

    const result = [];
    const seen = new Set();

    for (const el of candidates) {
      const text = norm(el.innerText || el.textContent);
      if (!text || !timeRe.test(text) || seen.has(text)) continue;
      seen.add(text);
      result.push(text);
    }

    return result;
  }

  diagnostics.timeElements = [...document.querySelectorAll('body *')].filter(el => {
    const text = norm(el.innerText || el.textContent);
    return text && timeRe.test(text);
  }).length;

  for (const channelAnchor of channelAnchors) {
    let channelHref = '';

    try {
      const u = new URL(channelAnchor.getAttribute('href'), location.href);
      if (!u.pathname.toLowerCase().includes('/kanallar/')) continue;
      channelHref = u.origin + u.pathname;
    } catch (_) {
      continue;
    }

    if (!channelHref || seenChannels.has(channelHref)) continue;

    let channelName = norm(
      channelAnchor.innerText || channelAnchor.textContent
    );

    if (!channelName) {
      const img = channelAnchor.querySelector('img[alt]');
      channelName = norm(img ? img.getAttribute('alt') : '');
    }

    channelName = channelName.replace(/^Kanal\s*:\s*/i, '').trim();
    if (!channelName) continue;

    let node = channelAnchor.parentElement;
    let chosenItems = [];
    let chosenDepth = 0;

    for (
      let depth = 1;
      depth <= 24 && node;
      depth++, node = node.parentElement
    ) {
      const channelLinks = [...node.querySelectorAll('a[href*="/kanallar/"]')];
      const uniqueHrefs = new Set();

      for (const link of channelLinks) {
        try {
          const u = new URL(link.getAttribute('href'), location.href);
          uniqueHrefs.add(u.origin + u.pathname);
        } catch (_) {}
      }

      if (uniqueHrefs.size > 1) continue;

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
          reason: 'Kanal kartında saat içeren program bağlantısı bulunamadı'
        });
      }

      continue;
    }

    seenChannels.add(channelHref);

    rows.push({
      name: channelName,
      href: channelHref,
      items: chosenItems,
      depth: chosenDepth
    });
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
    return normalize_name(name) in BAD_CHANNELS


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
        "",
        title,
        flags=re.IGNORECASE,
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
        "em": em,
    }


def date_label(day, today):
    if day == today:
        return "Bugün"

    if day == today + timedelta(days=1):
        return "Yarın"

    return day.strftime("%d.%m.%Y")


def find_date_link(page, label):
    links = page.locator("a[channeldatebegin][channeldateend]")

    for index in range(links.count()):
        link = links.nth(index)

        try:
            text = clean_text(link.inner_text(timeout=2000))

            if text == label:
                return link
        except Exception:
            continue

    return None


def select_date(page, label):
    link = find_date_link(page, label)

    if link is None:
        raise RuntimeError(
            f"Tivibu sayfasında '{label}' tarih bağlantısı bulunamadı."
        )

    before_url = page.url

    try:
        link.click(timeout=15000, force=True)
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=15000,
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(DATE_WAIT_MS)

    print(
        f"  Tarih seçildi: {label} "
        f"({'sayfa yenilendi' if page.url != before_url else 'sayfa üzerinde güncellendi'})",
        flush=True,
    )


def get_stable_html(page):
    """
    Canlı sayfada evaluate/evaluate_all çağırmaz.
    Sayfa HTML içeriğini Playwright'ın content() metodu ile alır.
    Gezinme sırasında hata oluşursa kısa süre bekleyip tekrar dener.
    """
    last_error = None

    for attempt in range(1, 7):
        try:
            try:
                page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=5000,
                )
            except PlaywrightTimeoutError:
                pass

            source = page.content()

            if source and len(source) > 5000:
                return source

            last_error = RuntimeError(
                f"Sayfa HTML içeriği beklenenden kısa: {len(source)} karakter"
            )

        except Exception as exc:
            last_error = exc

        print(
            f"  HTML okuma denemesi {attempt}/6 başarısız: {last_error}",
            flush=True,
        )

        page.wait_for_timeout(1500)

    raise RuntimeError(
        f"Tivibu sayfasının HTML içeriği okunamadı: {last_error}"
    )


def extract_from_html(browser, source):
    """
    Canlı sayfanın HTML kopyasını izole bir sekmede açar.
    Script etiketleri kaldırıldığı için Tivibu'nun otomatik yönlendirmeleri
    veya JavaScript işlemleri bu sekmede çalışmaz.
    """
    source = re.sub(
        r"<script\b[^>]*>.*?</script\s*>",
        "",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )

    source = re.sub(
        r"<base\b[^>]*>",
        "",
        source,
        flags=re.IGNORECASE,
    )

    isolated = browser.new_page(
        java_script_enabled=True,
        locale="tr-TR",
        timezone_id="Europe/Istanbul",
    )

    try:
        isolated.set_content(
            '<base href="https://www.tivibu.com.tr/">\n' + source,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        return isolated.evaluate(EXTRACT_JS)

    finally:
        isolated.close()


def assign_times(items, selected_day):
    parsed = [
        p for p in (parse_program_line(x) for x in items)
        if p
    ]

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
            tzinfo=ISTANBUL,
        )

        end_day = (
            current_day + timedelta(days=1)
            if item["eh"] * 60 + item["em"] <= start_minutes
            else current_day
        )

        stop = datetime(
            end_day.year,
            end_day.month,
            end_day.day,
            item["eh"],
            item["em"],
            tzinfo=ISTANBUL,
        )

        if (
            stop > start
            and (stop - start).total_seconds() <= 24 * 3600
        ):
            result.append({
                "title": item["title"],
                "start": start,
                "stop": stop,
            })

        previous_minutes = start_minutes

    return result


def collect_seven_days(browser, page, today):
    all_channels = {}
    all_programs = {}

    window_start = datetime(
        today.year,
        today.month,
        today.day,
        0,
        0,
        tzinfo=ISTANBUL,
    )

    window_end_day = today + timedelta(days=DAYS)

    window_end = datetime(
        window_end_day.year,
        window_end_day.month,
        window_end_day.day,
        0,
        0,
        tzinfo=ISTANBUL,
    )

    for offset in range(DAYS):
        selected_day = today + timedelta(days=offset)
        label = date_label(selected_day, today)

        print(
            f"\n[{offset + 1}/{DAYS}] {label} alınıyor...",
            flush=True,
        )

        if offset > 0:
            select_date(page, label)

        source = get_stable_html(page)

        extracted = extract_from_html(browser, source)

        rows = extracted.get("rows", [])
        diagnostics = extracted.get("diagnostics", {})

        print(
            f"  HTML kanal bağlantısı: "
            f"{diagnostics.get('channelAnchors', 0)}",
            flush=True,
        )

        print(
            f"  Eşleşen kanal kartı: "
            f"{diagnostics.get('mapped', 0)}",
            flush=True,
        )

        if not rows:
            raise RuntimeError(
                f"{label} için kanal/program kartı bulunamadı. "
                f"Kanal bağlantısı: "
                f"{diagnostics.get('channelAnchors', 0)}, "
                f"eşleşmeyen: {diagnostics.get('unmapped', 0)}. "
                "Mevcut XML korunuyor."
            )

        day_program_count = 0
        day_channel_count = 0

        for row in rows:
            channel_name = clean_text(row.get("name"))

            if not channel_name or is_bad_channel(channel_name):
                continue

            programs = assign_times(
                row.get("items", []),
                selected_day,
            )

            if not programs:
                continue

            if channel_name not in all_channels:
                all_channels[channel_name] = channel_name
                day_channel_count += 1

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
            f"  {len(rows)} kanal kartı, "
            f"{day_channel_count} yeni kanal, "
            f"{day_program_count} yeni program",
            flush=True,
        )

        if diagnostics.get("unmapped"):
            print(
                f"  Eşleşmeyen bağlantı: "
                f"{diagnostics['unmapped']}",
                flush=True,
            )

            for sample in diagnostics.get("samples", [])[:5]:
                print(
                    f"    {sample.get('name')} | "
                    f"{sample.get('reason')}",
                    flush=True,
                )

    return (
        sorted(all_channels.values(), key=str.casefold),
        list(all_programs.values()),
    )


def build_xml(channel_names, programs):
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
        },
    )

    for name in sorted(channel_names, key=str.casefold):
        channel = ET.SubElement(
            root,
            "channel",
            {"id": channel_ids[name]},
        )

        display = ET.SubElement(
            channel,
            "display-name",
            {"lang": "tr"},
        )

        display.text = name

    for item in sorted(
        programs,
        key=lambda x: (
            x["start"],
            x["channel_name"].casefold(),
            x["stop"],
        ),
    ):
        programme = ET.SubElement(
            root,
            "programme",
            {
                "channel": channel_ids[item["channel_name"]],
                "start": item["start"].strftime("%Y%m%d%H%M%S %z"),
                "stop": item["stop"].strftime("%Y%m%d%H%M%S %z"),
            },
        )

        title = ET.SubElement(
            programme,
            "title",
            {"lang": "tr"},
        )

        title.text = item["title"]

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    temp_file = OUTPUT_FILE + ".tmp"

    tree.write(
        temp_file,
        encoding="utf-8",
        xml_declaration=True,
    )

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
            f"En az {MIN_PROGRAMS} program bekleniyordu. "
            "Mevcut XML korunuyor."
        )

    days = {
        program["start"].date()
        for program in programs
    }

    if len(days) < 6:
        raise RuntimeError(
            f"Yalnızca {len(days)} farklı yayın günü bulundu. "
            "Eksik veriyle XML yazılmadı."
        )


def main():
    started = time.time()
    today = datetime.now(ISTANBUL).date()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("HTML yanıtından okuma")
    print("Canlı sayfada evaluate_all kullanılmıyor")
    print(f"Kaynak: {LIVE_TV_URL}")
    print(f"Başlangıç: {today.strftime('%d.%m.%Y')}")
    print("=" * 68)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1440,
                "height": 1100,
            },
        )

        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        print("[1/4] Tivibu sayfası açılıyor...", flush=True)

        page.goto(
            LIVE_TV_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=15000,
            )
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(2500)

        print("[2/4] Tarih seçenekleri kontrol ediliyor...", flush=True)

        date_count = page.locator(
            "a[channeldatebegin][channeldateend]"
        ).count()

        print(
            f"Bulunan tarih seçeneği: {date_count}",
            flush=True,
        )

        if date_count < DAYS:
            raise RuntimeError(
                f"Tivibu yalnızca {date_count} tarih seçeneği gösterdi."
            )

        print("[3/4] Kanal ve programlar alınıyor...", flush=True)

        channel_names, programs = collect_seven_days(
            browser,
            page,
            today,
        )

        context.close()
        browser.close()

    print("\n[4/4] Veriler kontrol ediliyor...", flush=True)

    validate_data(channel_names, programs)

    build_xml(channel_names, programs)

    days = sorted({
        program["start"].date()
        for program in programs
    })

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
