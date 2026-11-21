
import os
import re
import html
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, unquote
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"

OUTPUT_FILE = "epg.xml"
TEMP_FILE = "epg.xml.tmp"

ISTANBUL = ZoneInfo("Europe/Istanbul")

DAYS = 7
PAGE_TIMEOUT = 60000
PAGE_WAIT_MS = 700
DATE_WAIT_MS = 500

MIN_CHANNELS = 30

BAD_CHANNELS = {
    "BENİM KANALIM",
}

KNOWN_CHANNEL_IDS = {
    "DMAX": "ch00000000000000001124",
    "TRT 1": "ch00000000000000001266",
}


# ============================================================
# TARİH YARDIMCILARI
# ============================================================

def date_label(day, today):
    if day == today:
        return "Bugün"

    if day == today + timedelta(days=1):
        return "Yarın"

    return day.strftime("%d.%m.%Y")


def make_datetime(day, hour, minute):
    return datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute,
        tzinfo=ISTANBUL
    )


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
        timeout=12000,
        force=True
    )

    page.wait_for_timeout(DATE_WAIT_MS)


# ============================================================
# KANAL LİSTESİ
# ============================================================

GET_CHANNELS_JS = r"""() => {
    const clean = s =>
        (s || '').replace(/\s+/g, ' ').trim();

    const cards = [
        ...document.querySelectorAll(
            '.fullLink[channelcode][title]'
        )
    ];

    const result = [];
    const seen = new Set();

    for (const card of cards) {
        const name = clean(
            card.getAttribute('title')
        );

        const id = clean(
            card.getAttribute('channelcode')
        );

        const link = card.querySelector(
            'a[href^="/kanallar/"]'
        );

        if (!name || !id || !link) continue;

        const key = id.toLowerCase();

        if (seen.has(key)) continue;

        seen.add(key);

        result.push({
            name: name,
            channel_id: id,
            channel_url: link.getAttribute('href')
        });
    }

    return {
        card_count: cards.length,
        channels: result
    };
}"""


def get_channel_list(page):
    print("\nTüm Kanallar sayfası açılıyor...")

    page.goto(
        LIVE_TV_URL,
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

    page.wait_for_timeout(1200)

    result = page.evaluate(GET_CHANNELS_JS)

    channels = result.get("channels", [])

    print(
        f"Kanal kartı: {result.get('card_count', 0)}"
    )

    print(
        f"Benzersiz kanal ID: {len(channels)}"
    )

    print("\n===== KANAL LİSTESİ =====")

    for channel in channels:
        print(
            f"{channel['name']}: "
            f"{channel['channel_id']} | "
            f"{channel['channel_url']}"
        )

    if len(channels) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(channels)} kanal bulundu. "
            "Kanal listesi eksik olduğu için XML korunuyor."
        )

    return channels


# ============================================================
# PROGRAM HTML OKUMA
# ============================================================

GET_PROGRAMS_JS = r"""() => {
    const clean = s =>
        (s || '').replace(/\s+/g, ' ').trim();

    const anchors = [
        ...document.querySelectorAll('a')
    ];

    const programs = [];

    for (const a of anchors) {
        const titleElement = a.querySelector(
            '.programTitle'
        );

        const startElement = a.querySelector(
            '.startTime'
        );

        const finishElement = a.querySelector(
            '.finishTime'
        );

        if (
            !titleElement ||
            !startElement ||
            !finishElement
        ) {
            continue;
        }

        const title = clean(
            titleElement.textContent
        );

        const start = clean(
            startElement.textContent
        );

        const finish = clean(
            finishElement.textContent
        );

        if (!title || !start || !finish) {
            continue;
        }

        programs.push({
            title: title,
            start: start,
            finish: finish,
            href: a.getAttribute('href') || ''
        });
    }

    return programs;
}"""


TIME_RE = re.compile(
    r"^(\d{1,2}):(\d{2})$"
)


def parse_time(value):
    value = str(value or "").strip()

    match = TIME_RE.fullmatch(value)

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def get_channel_id_from_href(href):
    if not href:
        return None

    decoded = unquote(
        html.unescape(str(href))
    )

    match = re.search(
        r"(ch[a-z0-9_-]+)",
        decoded,
        re.IGNORECASE
    )

    if not match:
        return None

    return match.group(1)


def parse_programs(raw_programs, channel, selected_day):
    result = []

    expected_id = channel["channel_id"].lower()

    for raw in raw_programs:
        title = html.unescape(
            str(raw.get("title", "")).strip()
        )

        title = re.sub(
            r"\s+",
            " ",
            title
        ).strip()

        if not title:
            continue

        start_time = parse_time(
            raw.get("start")
        )

        finish_time = parse_time(
            raw.get("finish")
        )

        if not start_time or not finish_time:
            continue

        href_id = get_channel_id_from_href(
            raw.get("href", "")
        )

        # Program bağlantısında kanal ID'si varsa
        # mutlaka açık olan kanalın ID'siyle karşılaştır.
        if href_id and href_id.lower() != expected_id:
            continue

        sh, sm = start_time
        eh, em = finish_time

        start = make_datetime(
            selected_day,
            sh,
            sm
        )

        stop_day = selected_day

        if (eh * 60 + em) <= (sh * 60 + sm):
            stop_day += timedelta(days=1)

        stop = make_datetime(
            stop_day,
            eh,
            em
        )

        if stop <= start:
            continue

        if (stop - start).total_seconds() > 24 * 3600:
            continue

        result.append({
            "title": title,
            "start": start,
            "stop": stop,
            "channel_id": channel["channel_id"],
            "channel_name": channel["name"],
        })

    return result


# ============================================================
# TEK KANALIN 7 GÜNLÜK YAYIN AKIŞI
# ============================================================

def collect_channel(page, channel, today, window_start, window_end):
    name = channel["name"]
    channel_id = channel["channel_id"]

    known_id = KNOWN_CHANNEL_IDS.get(name)

    if known_id and known_id != channel_id:
        print(
            f"ID UYUŞMAZLIĞI: {name} | "
            f"{channel_id} | Beklenen: {known_id}"
        )
        return []

    channel_url = urljoin(
        BASE_URL,
        channel["channel_url"]
    )

    print(
        f"\nKANAL: {name} | {channel_id}"
    )

    try:
        page.goto(
            channel_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=8000
            )
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(PAGE_WAIT_MS)

    except Exception as exc:
        print(
            f"  Kanal sayfası açılamadı: {exc}"
        )
        return []

    channel_programs = {}

    for offset in range(DAYS):
        selected_day = today + timedelta(days=offset)

        label = date_label(
            selected_day,
            today
        )

        try:
            select_date(
                page,
                label
            )

        except Exception as exc:
            print(
                f"  {label}: Tarih seçilemedi: {exc}"
            )
            continue

        try:
            raw_programs = page.evaluate(
                GET_PROGRAMS_JS
            )

        except Exception as exc:
            print(
                f"  {label}: Programlar okunamadı: {exc}"
            )
            continue

        parsed = parse_programs(
            raw_programs,
            channel,
            selected_day
        )

        added = 0

        for program in parsed:
            if (
                program["start"] < window_start
                or program["start"] >= window_end
            ):
                continue

            key = (
                program["channel_id"].lower(),
                program["title"].casefold(),
                program["start"],
                program["stop"]
            )

            if key not in channel_programs:
                channel_programs[key] = program
                added += 1

        print(
            f"  {label}: "
            f"{len(parsed)} program okundu, "
            f"{added} yeni kayıt"
        )

    result = list(channel_programs.values())

    print(
        f"  SONUÇ: {name} - "
        f"{len(result)} program"
    )

    return result


# ============================================================
# BÜTÜN KANALLARI TOPLA
# ============================================================

def collect_all(page, today):
    channels = get_channel_list(page)

    window_start = make_datetime(
        today,
        0,
        0
    )

    end_day = today + timedelta(days=DAYS)

    window_end = make_datetime(
        end_day,
        0,
        0
    )

    all_programs = {}

    successful_channels = set()

    failed_channels = []

    total = len(channels)

    print("\n" + "=" * 68)
    print("KANALLARIN YAYIN AKIŞLARI TEK TEK OKUNUYOR")
    print(f"Toplam kanal: {total}")
    print(f"Toplam gün: {DAYS}")
    print("=" * 68)

    for index, channel in enumerate(channels, start=1):
        print(
            f"\n[{index}/{total}] "
            f"{channel['name']}"
        )

        if channel["name"].upper() in BAD_CHANNELS:
            print("  Atlandı.")
            continue

        programs = collect_channel(
            page,
            channel,
            today,
            window_start,
            window_end
        )

        if not programs:
            failed_channels.append(
                channel["name"]
            )
            continue

        successful_channels.add(
            channel["channel_id"]
        )

        for program in programs:
            key = (
                program["channel_id"].lower(),
                program["title"].casefold(),
                program["start"],
                program["stop"]
            )

            all_programs[key] = program

    return (
        channels,
        list(all_programs.values()),
        successful_channels,
        failed_channels
    )


# ============================================================
# VERİ KONTROLÜ
# ============================================================

def validate_data(channels, programs, successful_channels):
    if len(successful_channels) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(successful_channels)} kanalın "
            f"yayın akışı bulundu. Minimum gerekli kanal: "
            f"{MIN_CHANNELS}. Mevcut XML korunuyor."
        )

    if not programs:
        raise RuntimeError(
            "Hiç program bulunamadı. Mevcut XML korunuyor."
        )

    channel_ids = {
        channel["channel_id"]
        for channel in channels
    }

    for program in programs:
        if program["channel_id"] not in channel_ids:
            raise RuntimeError(
                "Programın kanal ID'si kanal listesinde yok: "
                + program["channel_id"]
            )

        if program["stop"] <= program["start"]:
            raise RuntimeError(
                "Geçersiz program saati: "
                + program["title"]
            )

    days = {
        program["start"].date()
        for program in programs
    }

    if len(days) < 6:
        raise RuntimeError(
            f"Yalnızca {len(days)} farklı yayın günü bulundu. "
            "Mevcut XML korunuyor."
        )

    # Aynı ID birden fazla kanal adıyla kullanılmasın.
    id_names = {}

    for channel in channels:
        channel_id = channel["channel_id"]
        name = channel["name"]

        if (
            channel_id in id_names
            and id_names[channel_id] != name
        ):
            raise RuntimeError(
                f"Kanal ID çakışması: {channel_id}"
            )

        id_names[channel_id] = name


# ============================================================
# XML OLUŞTUR
# ============================================================

def build_xml(channels, programs, successful_channels):
    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL
        }
    )

    used_channels = [
        channel
        for channel in channels
        if channel["channel_id"] in successful_channels
    ]

    for channel in used_channels:
        channel_element = ET.SubElement(
            root,
            "channel",
            {
                "id": channel["channel_id"]
            }
        )

        display = ET.SubElement(
            channel_element,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display.text = channel["name"]

    for item in sorted(
        programs,
        key=lambda x: (
            x["start"],
            x["channel_name"].casefold()
        )
    ):
        programme = ET.SubElement(
            root,
            "programme",
            {
                "channel": item["channel_id"],
                "start": item["start"].strftime(
                    "%Y%m%d%H%M%S %z"
                ),
                "stop": item["stop"].strftime(
                    "%Y%m%d%H%M%S %z"
                )
            }
        )

        title = ET.SubElement(
            programme,
            "title",
            {
                "lang": "tr"
            }
        )

        title.text = item["title"]

    tree = ET.ElementTree(root)

    ET.indent(
        tree,
        space="  "
    )

    # Önce geçici dosyaya yaz.
    tree.write(
        TEMP_FILE,
        encoding="utf-8",
        xml_declaration=True
    )

    # XML dosyasının gerçekten okunabildiğini doğrula.
    ET.parse(TEMP_FILE)

    # Ancak doğrulamadan sonra asıl dosyanın üzerine yaz.
    os.replace(
        TEMP_FILE,
        OUTPUT_FILE
    )


# ============================================================
# ANA PROGRAM
# ============================================================

def main():
    started = time.time()

    today = datetime.now(
        ISTANBUL
    ).date()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("Yalnızca TÜM KANALLAR")
    print("Her kanalın kendi yayın akışı ayrı ayrı okunacak.")
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

        try:
            (
                channels,
                programs,
                successful_channels,
                failed_channels
            ) = collect_all(
                page,
                today
            )

        finally:
            browser.close()

    validate_data(
        channels,
        programs,
        successful_channels
    )

    build_xml(
        channels,
        programs,
        successful_channels
    )

    days = sorted({
        program["start"].date()
        for program in programs
    })

    print("\n" + "=" * 68)
    print("TIVIBU EPG BAŞARIYLA OLUŞTURULDU")
    print("=" * 68)

    print(
        f"Bulunan kanal kartı: {len(channels)}"
    )

    print(
        f"Yayın akışı alınan kanal: "
        f"{len(successful_channels)}"
    )

    print(
        f"Yayın akışı alınamayan kanal: "
        f"{len(failed_channels)}"
    )

    print(
        f"Toplam program: {len(programs)}"
    )

    print(
        f"Program tarihi sayısı: {len(days)}"
    )

    if days:
        print(
            f"İlk tarih: {days[0]}"
        )

        print(
            f"Son tarih: {days[-1]}"
        )

    if failed_channels:
        print("\nYAYIN AKIŞI ALINAMAYAN KANALLAR:")

        for name in failed_channels:
            print(f"  {name}")

    print(
        f"\nDosya: {OUTPUT_FILE}"
    )

    print(
        f"Süre: {time.time() - started:.1f} saniye"
    )

    print("=" * 68)


if __name__ == "__main__":
    main()
