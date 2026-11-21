
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

# Yalnızca Tüm Kanallar sayfası taranacak.
CATEGORY_PAGES = [
    ("Tüm Kanallar", "/canli-tv"),
]

BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
    "TİVİBU TANITIM SAYFASI",
}

# F12 üzerinden doğrulanmış ID.
KNOWN_CHANNEL_IDS = {
    "DMAX": "ch00000000000000001124",
}

TIME_RE = re.compile(
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})\s*(?:→|➜|[-–])\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})"
)

CATEGORY_RE = re.compile(
    r"\s+(?:Yaşam|Film|Dizi|Spor|Haber|Çocuk|Müzik|Belgesel|Sinema|Ulusal|Global|Diğer|Aktüalite|Spor Programı)\s*[-–]?\s*$",
    re.IGNORECASE,
)


# Kanal kartlarını ve bütün sayfadaki program bağlantılarını
# gerçek Tivibu kanal ID'si üzerinden eşleştirir.
EXTRACT_JS = r"""() => {
    const norm = s => (s || '').replace(/\s+/g, ' ').trim();

    const timeRe =
        /\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}/;

    const getChannelId = href => {
        if (!href) return '';

        let decoded = href;

        try {
            decoded = decodeURIComponent(href);
        } catch (e) {}

        const match = decoded.match(
            /(?:\||%7c)(ch[a-z0-9_-]+)/i
        );

        return match ? match[1] : '';
    };

    // Önce bütün gerçek kanal kartlarını oku.
    const cards = [
        ...document.querySelectorAll(
            '.fullLink[channelcode][title]'
        )
    ];

    const channelMap = new Map();

    for (const card of cards) {
        const name = norm(card.getAttribute('title'));
        const channelId = norm(
            card.getAttribute('channelcode')
        );

        if (!name || !channelId) continue;

        const key = channelId.toLowerCase();

        if (!channelMap.has(key)) {
            channelMap.set(key, {
                name,
                channel_id: channelId
            });
        }
    }

    // Sayfadaki bütün program bağlantılarını tara.
    // Program bağlantıları kanal kartının dışında da olabilir.
    const groups = new Map();

    const anchors = [
        ...document.querySelectorAll('a')
    ];

    let timeLinkCount = 0;
    let unmatchedLinkCount = 0;

    const unmatchedIds = new Set();

    for (const a of anchors) {
        const text = norm(a.innerText || a.textContent);

        if (!timeRe.test(text)) continue;

        timeLinkCount++;

        let channelId = getChannelId(a.href);

        // URL'den ID alınamazsa en yakın kanal kartını kontrol et.
        if (!channelId) {
            const card = a.closest(
                '.fullLink[channelcode][title]'
            );

            if (card) {
                channelId = norm(
                    card.getAttribute('channelcode')
                );
            }
        }

        if (!channelId) {
            unmatchedLinkCount++;
            continue;
        }

        const key = channelId.toLowerCase();

        if (!channelMap.has(key)) {
            unmatchedIds.add(channelId);
            continue;
        }

        if (!groups.has(key)) {
            groups.set(key, []);
        }

        groups.get(key).push(text);
    }

    // Her kanalın adı karttan, programları bağlantılardan gelir.
    const rows = [];

    for (const [key, items] of groups.entries()) {
        const channel = channelMap.get(key);

        if (!channel || !items.length) continue;

        rows.push({
            name: channel.name,
            channel_id: channel.channel_id,
            items
        });
    }

    return {
        rows,
        cards: [...channelMap.values()],
        diagnostics: {
            cardCount: cards.length,
            uniqueChannelCount: channelMap.size,
            timeLinkCount,
            matchedChannelCount: rows.length,
            unmatchedLinkCount,
            unmatchedChannelIds: [...unmatchedIds],
            sampleCards: [...channelMap.values()].slice(0, 10),
            sampleRows: rows.slice(0, 5).map(row => ({
                name: row.name,
                channel_id: row.channel_id,
                programCount: row.items.length,
                firstProgram: row.items[0]
            }))
        }
    };
}"""


def clean_text(value):
    value = html.unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_channel_name(value):
    return clean_text(value).casefold()


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

    page.wait_for_timeout(1500)


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

        for offset in range(DAYS):
            selected_day = today + timedelta(days=offset)
            label = date_label(selected_day, today)

            print(f"\n  {label} taranıyor...")

            select_date(page, label)

            extracted = page.evaluate(EXTRACT_JS)

            rows = extracted.get("rows", [])
            cards = extracted.get("cards", [])
            diagnostics = extracted.get("diagnostics", {})

            print(
                f"  Kanal kartı: "
                f"{diagnostics.get('cardCount', 0)}"
            )

            print(
                f"  Benzersiz kanal ID: "
                f"{diagnostics.get('uniqueChannelCount', 0)}"
            )

            print(
                f"  Program bağlantısı: "
                f"{diagnostics.get('timeLinkCount', 0)}"
            )

            print(
                f"  Eşleşen kanal: "
                f"{diagnostics.get('matchedChannelCount', 0)}"
            )

            if offset == 0:
                print("\n===== GERÇEK KANAL ID LİSTESİ =====")

                for card in cards:
                    print(
                        f"  {card['name']}: "
                        f"{card['channel_id']}"
                    )

                print("\n===== EŞLEŞTİRME ÖRNEKLERİ =====")

                for sample in diagnostics.get(
                    "sampleRows", []
                ):
                    print(
                        f"  {sample['name']} | "
                        f"{sample['channel_id']} | "
                        f"{sample['programCount']} program"
                    )

                if diagnostics.get("unmatchedChannelIds"):
                    print(
                        "\nKartlarda bulunamayan bağlantı ID'leri:",
                        diagnostics["unmatchedChannelIds"]
                    )

            if not rows:
                print(
                    "  UYARI: Kanal-program eşleşmesi bulunamadı."
                )
                continue

            day_program_count = 0

            for row in rows:
                channel_name = clean_text(row.get("name"))
                channel_id = clean_text(
                    row.get("channel_id")
                )

                if (
                    not channel_name
                    or channel_name.upper() in BAD_CHANNELS
                ):
                    continue

                if not re.fullmatch(
                    r"ch[a-z0-9_-]+",
                    channel_id,
                    re.IGNORECASE
                ):
                    print(
                        f"  GEÇERSİZ ID: "
                        f"{channel_name} | {channel_id}"
                    )
                    continue

                known_id = KNOWN_CHANNEL_IDS.get(
                    channel_name.upper()
                )

                if known_id and channel_id != known_id:
                    print(
                        f"  ID UYUŞMAZLIĞI: {channel_name} "
                        f"| Kart: {channel_id} "
                        f"| F12: {known_id}"
                    )
                    continue

                programs = assign_times(
                    row.get("items", []),
                    selected_day
                )

                if not programs:
                    continue

                name_key = normalize_channel_name(channel_name)

                existing = all_channels.get(name_key)

                if existing and existing["id"] != channel_id:
                    raise RuntimeError(
                        f"Kanal ID çakışması: {channel_name} "
                        f"({existing['id']} / {channel_id})"
                    )

                all_channels[name_key] = {
                    "name": channel_name,
                    "id": channel_id
                }

                for program in programs:
                    if (
                        program["stop"] <= window_start
                        or program["start"] >= window_end
                    ):
                        continue

                    key = (
                        channel_id.casefold(),
                        program["title"].casefold(),
                        program["start"],
                        program["stop"],
                    )

                    if key not in all_programs:
                        all_programs[key] = {
                            "channel_name": channel_name,
                            "channel_id": channel_id,
                            **program,
                        }

                        day_program_count += 1

            print(
                f"  {len(rows)} eşleşen kanal / "
                f"{len(all_channels)} toplam kanal / "
                f"{day_program_count} yeni program"
            )

    channels = sorted(
        all_channels.values(),
        key=lambda x: x["name"].casefold()
    )

    return channels, list(all_programs.values())


def build_xml(channels, programs):
    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL,
        }
    )

    for channel in channels:
        channel_element = ET.SubElement(
            root,
            "channel",
            {"id": channel["id"]}
        )

        display = ET.SubElement(
            channel_element,
            "display-name",
            {"lang": "tr"}
        )

        display.text = channel["name"]

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
                "channel": item["channel_id"],
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


def validate_data(channels, programs):
    if not channels:
        raise RuntimeError(
            "Hiç kanal bulunamadı. Mevcut XML korunuyor."
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

    ids = [channel["id"] for channel in channels]

    if len(ids) != len(set(ids)):
        raise RuntimeError(
            "Aynı kanal ID'si birden fazla kanalda kullanılmış. "
            "Mevcut XML korunuyor."
        )

    valid_ids = set(ids)

    for program in programs:
        if program["channel_id"] not in valid_ids:
            raise RuntimeError(
                f"Programın kanal ID'si XML kanal listesinde yok: "
                f"{program['channel_id']}"
            )


def main():
    started = time.time()
    today = datetime.now(ISTANBUL).date()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG - YALNIZCA TÜM KANALLAR")
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

        channels, programs = collect_all(
            page,
            today
        )

        browser.close()

    validate_data(
        channels,
        programs
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
    print(f"Gerçek kanal ID sayısı: {len(channels)}")
    print(f"Toplam program: {len(programs)}")
    print(f"Program tarihi sayısı: {len(days)}")
    print(f"İlk tarih: {days[0]}")
    print(f"Son tarih: {days[-1]}")
    print(f"Dosya: {OUTPUT_FILE}")
    print(f"Süre: {time.time() - started:.1f} saniye")
    print("=" * 68)


if __name__ == "__main__":
    main()
