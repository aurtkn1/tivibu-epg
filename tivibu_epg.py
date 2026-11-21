
import re
import html
import os
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = BASE_URL + "/canli-tv"
OUTPUT_FILE = "epg.xml"

ISTANBUL = ZoneInfo("Europe/Istanbul")
DAYS = 7

PAGE_TIMEOUT = 60000
DATE_WAIT_MS = 2500

MIN_CHANNELS = 30
MIN_PROGRAMS = 100

TIME_RE = re.compile(
    r"\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}"
)

BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
    "TİVİBU TANITIM SAYFASI",
    "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
    "TİVİBU NEDİR?",
}

CATEGORY_RE = re.compile(
    r"\s+(?:Yaşam|Film|Dizi|Spor|Haber|Çocuk|Müzik|Belgesel|"
    r"Sinema|Ulusal|Global|Diğer|Aktüalite|Spor Programı)\s*[-–]?\s*$",
    re.IGNORECASE,
)


# Kanal ve program bağlantılarını üst kapsayıcıya göre değil,
# aynı kanal URL'sine göre eşleştirir.
EXTRACT_JS = r"""() => {
    const timeRe = /\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}/;
    const norm = s => (s || '').replace(/\s+/g, ' ').trim();

    function canonicalHref(raw) {
        try {
            const u = new URL(raw, location.href);
            if (!u.pathname.toLowerCase().includes('/kanallar/')) return '';
            return u.origin + u.pathname.replace(/\/+$/, '');
        } catch (_) {
            return '';
        }
    }

    const groups = new Map();
    const diagnostics = {
        totalLinks: 0,
        channelNames: 0,
        programLinks: 0,
        uniqueChannels: 0,
        matchedChannels: 0,
        samples: []
    };

    const anchors = [...document.querySelectorAll('a[href]')];

    for (const a of anchors) {
        const href = canonicalHref(a.getAttribute('href'));
        if (!href) continue;

        diagnostics.totalLinks++;

        let text = norm(a.innerText || a.textContent);

        if (!text) {
            const img = a.querySelector('img[alt]');
            if (img) text = norm(img.getAttribute('alt'));
        }

        if (!text) continue;

        if (!groups.has(href)) {
            groups.set(href, {
                name: '',
                programs: [],
                seenPrograms: new Set()
            });
        }

        const group = groups.get(href);

        if (timeRe.test(text)) {
            diagnostics.programLinks++;

            if (!group.seenPrograms.has(text)) {
                group.seenPrograms.add(text);
                group.programs.push(text);
            }
        } else {
            text = text.replace(/^Kanal\s*:\s*/i, '').trim();

            // Program adı veya boş metin kanal adı olarak kullanılmasın.
            if (text && !group.name) {
                group.name = text;
                diagnostics.channelNames++;
            }
        }
    }

    const rows = [];

    for (const [href, group] of groups.entries()) {
        if (!group.name || !group.programs.length) {
            if (diagnostics.samples.length < 15) {
                diagnostics.samples.push({
                    href: href,
                    name: group.name,
                    programCount: group.programs.length
                });
            }
            continue;
        }

        rows.push({
            name: group.name,
            href: href,
            items: group.programs
        });
    }

    diagnostics.uniqueChannels = groups.size;
    diagnostics.matchedChannels = rows.length;

    return {
        rows: rows,
        diagnostics: diagnostics
    };
}"""


def clean_text(value):
    value = html.unescape(str(value or ""))
    value = value.replace("\xa0", " ")
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
        value = value.replace(old, new)
        value = value.replace(old.upper(), new)

    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
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

    match = re.search(
        r"(?P<sh>\d{1,2}):(?P<sm>\d{2})\s*"
        r"(?:→|➜|[-–])\s*"
        r"(?P<eh>\d{1,2}):(?P<em>\d{2})",
        line,
    )

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

    title = re.sub(r"\s+Canlı$", "", title, flags=re.IGNORECASE).strip()

    if not title:
        return None

    sh = int(match.group("sh"))
    sm = int(match.group("sm"))
    eh = int(match.group("eh"))
    em = int(match.group("em"))

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


def select_date(page, label):
    links = page.locator("a[channeldatebegin][channeldateend]")

    selected = None

    for i in range(links.count()):
        link = links.nth(i)

        try:
            text = clean_text(link.inner_text(timeout=2000))

            if text == label:
                selected = link
                break
        except Exception:
            continue

    if selected is None:
        raise RuntimeError(
            f"'{label}' tarih seçeneği bulunamadı."
        )

    try:
        selected.click(timeout=15000, force=True)
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=10000,
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(DATE_WAIT_MS)


def get_page_html(page):
    last_error = None

    for attempt in range(1, 7):
        try:
            source = page.content()

            if source and len(source) > 5000:
                return source

            last_error = RuntimeError(
                f"HTML çok kısa: {len(source)} karakter"
            )

        except Exception as exc:
            last_error = exc

        print(
            f"  HTML okuma denemesi {attempt}/6: {last_error}",
            flush=True,
        )

        page.wait_for_timeout(1200)

    raise RuntimeError(
        f"Sayfa HTML içeriği okunamadı: {last_error}"
    )


def extract_from_html(browser, source):
    # Scriptleri kaldırarak izole sayfada otomatik yönlendirme
    # ve sayfa yenileme işlemlerini engelle.
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
        item
        for item in (parse_program_line(x) for x in items)
        if item
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

    end_day = today + timedelta(days=DAYS)

    window_end = datetime(
        end_day.year,
        end_day.month,
        end_day.day,
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

        source = get_page_html(page)
        extracted = extract_from_html(browser, source)

        rows = extracted.get("rows", [])
        diagnostics = extracted.get("diagnostics", {})

        print(
            f"  Toplam kanal bağlantısı: "
            f"{diagnostics.get('totalLinks', 0)}"
        )

        print(
            f"  Farklı kanal adresi: "
            f"{diagnostics.get('uniqueChannels', 0)}"
        )

        print(
            f"  Kanal adı bulunan: "
            f"{diagnostics.get('channelNames', 0)}"
        )

        print(
            f"  Program bağlantısı: "
            f"{diagnostics.get('programLinks', 0)}"
        )

        print(
            f"  Eşleşen kanal: "
            f"{diagnostics.get('matchedChannels', 0)}"
        )

        if not rows:
            print("  Eşleşme örnekleri:")

            for sample in diagnostics.get("samples", [])[:10]:
                print(
                    f"    URL: {sample.get('href')} | "
                    f"Ad: {sample.get('name')} | "
                    f"Program: {sample.get('programCount')}"
                )

            raise RuntimeError(
                f"{label} için kanal-program eşleşmesi bulunamadı. "
                "Mevcut XML korunuyor."
            )

        day_new_channels = 0
        day_new_programs = 0

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
                day_new_channels += 1

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

                    day_new_programs += 1

        print(
            f"  {len(rows)} eşleşen kanal, "
            f"{day_new_channels} yeni kanal, "
            f"{day_new_programs} yeni program",
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
        item["start"].date()
        for item in programs
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
    print("Kanal URL'si üzerinden kanal-program eşleştirme")
    print(f"Kaynak: {LIVE_TV_URL}")
    print(f"Başlangıç: {today.strftime('%d.%m.%Y')}")
    print("=" * 68)

    with sync_playwright() as p:
        browser = p.chromium.launch(
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

        print("[2/4] Tarih seçenekleri okunuyor...", flush=True)

        date_count = page.locator(
            "a[channeldatebegin][channeldateend]"
        ).count()

        print(
            f"Bulunan tarih seçeneği: {date_count}",
            flush=True,
        )

        if date_count < DAYS:
            raise RuntimeError(
                f"Yalnızca {date_count} tarih seçeneği bulundu."
            )

        print("[3/4] Kanal ve programlar alınıyor...", flush=True)

        channel_names, programs = collect_seven_days(
            browser,
            page,
            today,
        )

        context.close()
        browser.close()

    print("\n[4/4] Veriler doğrulanıyor...", flush=True)

    validate_data(channel_names, programs)
    build_xml(channel_names, programs)

    days = sorted({
        item["start"].date()
        for item in programs
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
