
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

# Yalnızca Tüm Kanallar sayfası
CATEGORY_PAGES = [
    ("Tüm Kanallar", "/canli-tv"),
]

BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
    "TİVİBU TANITIM SAYFASI",
}

# F12 ile doğrulanmış kanal ID'si
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


# Sayfadaki gerçek kanal kartlarını ve program bağlantılarını okur.
EXTRACT_JS = r"""() => {
    const norm = s => (s || '').replace(/\s+/g, ' ').trim();

    const timeRe = /\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}/;

    const cards = [
        ...document.querySelectorAll(
            '.fullLink[channelcode][title]'
        )
    ];

    const cardData = cards.map(card => ({
        name: norm(card.getAttribute('title')),
        channel_id: norm(card.getAttribute('channelcode'))
    }));

    const rows = [];

    for (const card of cards) {
        const name = norm(card.getAttribute('title'));
        const channelId = norm(card.getAttribute('channelcode'));

        if (!name || !channelId) continue;

        const links = [...card.querySelectorAll('a')];

        const programLinks = links.filter(a =>
            timeRe.test(norm(a.innerText || a.textContent))
        );

        const items = programLinks.map(a =>
            norm(a.innerText || a.textContent)
        );

        if (items.length) {
            rows.push({
                name,
                channel_id: channelId,
                items
            });
        }
    }

    // Bazı sayfa yapılarında program bağlantıları
    // kanal kartının dışında bulunabilir.
    // Bu durumda program bağlantısı href'i üzerinden
    // eski gruplama yöntemi kullanılır.
    if (rows.length === 0) {
        const groups = new Map();

        for (const a of document.querySelectorAll('a')) {
            const text = norm(a.innerText || a.textContent);

            if (!a.href || !timeRe.test(text)) continue;

            if (!groups.has(a.href)) {
                groups.set(a.href, []);
            }

            groups.get(a.href).push(text);
        }

        const logoNames = [];

        for (const img of document.querySelectorAll('img[alt]')) {
            const alt = norm(img.getAttribute('alt'));

            if (!/^Kanal\s*:/i.test(alt)) continue;

            const name = alt.replace(/^Kanal\s*:\s*/i, '').trim();

            if (name && !logoNames.includes(name)) {
                logoNames.push(name);
            }
        }

        const usableNames = logoNames.filter(name =>
            ![
                'BENİM KANALIM',
                'FAVORİ KANALLARIM'
            ].includes(name.toLocaleUpperCase('tr-TR'))
        );

        let index = 0;

        for (const [href, items] of groups.entries()) {
            let name = '';
            let channelId = '';

            const firstLink = [...document.querySelectorAll('a')]
                .find(a => a.href === href);

            let node = firstLink;

            for (
                let depth = 0;
                depth < 14 && node;
                depth++, node = node.parentElement
            ) {
                const card = node.matches(
                    '.fullLink[channelcode][title]'
                )
                    ? node
                    : node.querySelector(
                        '.fullLink[channelcode][title]'
                    );

                if (card) {
                    name = norm(card.getAttribute('title'));
                    channelId = norm(
                        card.getAttribute('channelcode')
                    );
                    break;
                }

                const imgs = [
                    ...node.querySelectorAll('img[alt]')
                ].map(img =>
                    norm(img.getAttribute('alt'))
                ).filter(alt =>
                    /^Kanal\s*:/i.test(alt)
                );

                if (imgs.length === 1) {
                    name = imgs[0]
                        .replace(/^Kanal\s*:\s*/i, '')
                        .trim();
                }
            }

            if (!name && index < usableNames.length) {
                name = usableNames[index];
            }

            index++;

            if (name && channelId && items.length) {
                rows.push({
                    name,
                    channel_id: channelId,
                    items
                });
            }
        }
    }

    return {
        rows,
        cards: cardData,
        diagnostics: {
            cardCount: cards.length,
            cardIdCount: cardData.filter(
                x => x.channel_id
            ).length,
            programRowCount: rows.length,
            sampleCards: cardData.slice(0, 10)
        }
    };
}"""


def clean_text(value):
    value = html.unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_channel_name(value):
    return clean_text(value).casefold()


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
        open_category(page, category_name, path)

        for offset in range(DAYS):
            selected_day = today + timedelta(days=offset)
            label = date_label(selected_day, today)

            print(f"\n  {label} taranıyor...")

            select_date(page, label)

            extracted = page.evaluate(EXTRACT_JS)

            rows = extracted.get("rows", [])
            cards = extracted.get("cards", [])

            print(
                f"  Kanal kartı: "
                f"{extracted.get('diagnostics', {}).get('cardCount', 0)}"
            )

            if offset == 0:
                print("\n  GERÇEK TİVİBU KANAL ID LİSTESİ")

                seen = set()

                for card in cards:
                    name = clean_text(card.get("name"))
                    channel_id = clean_text(
                        card.get("channel_id")
                    )

                    if (
                        name
                        and channel_id
                        and channel_id not in seen
                    ):
                        seen.add(channel_id)
                        print(f"  {name}: {channel_id}")

            if not rows:
                print(
                    "  UYARI: Program satırı bulunamadı."
                )
                print(
                    "  Tanılama:",
                    extracted.get("diagnostics", {})
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
                        f"  GEÇERSİZ ID: {channel_name} "
                        f"| {channel_id}"
                    )
                    continue

                # DMAX için F12'den doğrulanmış ID.
                known_id = KNOWN_CHANNEL_IDS.get(
                    channel_name.upper()
                )

                if known_id and channel_id != known_id:
                    print(
                        f"  ID UYUŞMAZLIĞI: {channel_name} "
                        f"| Kart: {channel_id} "
                        f"| F12: {known_id}"
                    )
                    channel_id = known_id

                programs = assign_times(
                    row.get("items", []),
                    selected_day
                )

                if not programs:
                    continue

                name_key = normalize_channel_name(channel_name)

                existing = all_channels.get(name_key)

                if existing and existing["id"] != channel_id:
                    print(
                        f"  UYARI: {channel_name} için farklı ID geldi: "
                        f"{existing['id']} / {channel_id}"
                    )
                    continue

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
                f"  {len(rows)} program grubu / "
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

    channel_ids = {
        normalize_channel_name(channel["name"]): channel["id"]
        for channel in channels
    }

    for channel in channels:
        ET.SubElement(
            root,
            "channel",
            {"id": channel["id"]}
        )

        display = root[-1]
        display_name = ET.SubElement(
            display,
            "display-name",
            {"lang": "tr"}
        )

        display_name.text = channel["name"]

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

    validate_data(channels, programs)

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
