
import re
import html
import time
import unicodedata
import os
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
DATE_WAIT_MS = 1300
MIN_CHANNELS = 30
MIN_PROGRAMS = 100

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


# Program bağlantısından başlayarak DOM'daki en yakın kanal bağlantısını
# arar. Önceki sürümlerdeki hatalı "tek kanal içeren üst kapsayıcı"
# ve "global en yakın bağlantı" yöntemleri kullanılmaz.
EXTRACT_JS = r"""() => {
    const timeRe = /\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}/;
    const norm = s => (s || "").replace(/\s+/g, " ").trim();

    const allAnchors = [...document.querySelectorAll("a[href]")];

    const channels = [];
    const programs = [];

    for (const a of allAnchors) {
        let url;

        try {
            url = new URL(a.href, location.href);
        } catch (_) {
            continue;
        }

        const path = url.pathname.toLowerCase();

        if (path.includes("/kanallar/")) {
            let name = norm(a.innerText || a.textContent);

            if (!name) {
                const img = a.querySelector("img[alt]");
                if (img) name = norm(img.getAttribute("alt"));
            }

            name = name.replace(/^Kanal\s*:\s*/i, "").trim();

            if (name) {
                channels.push({
                    element: a,
                    href: url.origin + url.pathname,
                    name
                });
            }
        }

        const text = norm(a.innerText || a.textContent);

        if (timeRe.test(text)) {
            programs.push({
                element: a,
                text
            });
        }
    }

    const rows = new Map();
    const assignedPrograms = new Set();
    const assignedChannels = new Set();

    const ancestors = element => {
        const result = [];
        let node = element;

        for (let depth = 0; depth < 30 && node; depth++) {
            result.push(node);
            node = node.parentElement;
        }

        return result;
    };

    const channelAncestorCache = new Map();

    function getChannelLinks(node) {
        if (channelAncestorCache.has(node)) {
            return channelAncestorCache.get(node);
        }

        const result = [];

        for (const a of node.querySelectorAll('a[href*="/kanallar/"]')) {
            try {
                const u = new URL(a.href, location.href);

                if (u.pathname.toLowerCase().includes("/kanallar/")) {
                    let name = norm(a.innerText || a.textContent);

                    if (!name) {
                        const img = a.querySelector("img[alt]");
                        if (img) name = norm(img.getAttribute("alt"));
                    }

                    name = name.replace(/^Kanal\s*:\s*/i, "").trim();

                    if (name) {
                        result.push({
                            href: u.origin + u.pathname,
                            name,
                            element: a
                        });
                    }
                }
            } catch (_) {}
        }

        channelAncestorCache.set(node, result);
        return result;
    }

    // Önce programın kendi üst elemanlarında kanal bağlantısı ara.
    // Program ve kanal farklı kardeş elemanlardaysa ortak kart kapsayıcısı
    // bulunur. Bir kapsayıcıda birden çok kanal varsa bir alt seviyeye
    // inilerek programın ait olduğu kanal belirlenir.
    for (const program of programs) {
        const chain = ancestors(program.element);
        let best = null;

        for (let depth = 0; depth < chain.length; depth++) {
            const node = chain[depth];
            const links = getChannelLinks(node);

            if (!links.length) continue;

            const unique = new Map();

            for (const item of links) {
                if (!unique.has(item.href)) {
                    unique.set(item.href, item);
                }
            }

            // Tek kanal bağlantısı olan ilk ortak kapsayıcı kesin eşleşme.
            if (unique.size === 1) {
                best = [...unique.values()][0];
                break;
            }

            // Birden fazla kanal varsa bu kapsayıcı henüz kart değildir.
            // Üst kapsayıcıya çıkmaya devam etme; yanlış eşleşme yaratır.
        }

        if (!best) continue;

        if (!rows.has(best.href)) {
            rows.set(best.href, {
                name: best.name,
                href: best.href,
                items: [],
                seen: new Set()
            });
        }

        const row = rows.get(best.href);

        if (!row.seen.has(program.text)) {
            row.seen.add(program.text);
            row.items.push(program.text);
        }

        assignedPrograms.add(program.element);
        assignedChannels.add(best.href);
    }

    // İkinci geçiş: Kanal kartlarının DOM sırasına göre programları
    // bölümlere ayır. Kanal bağlantısı programın üstünde değil, kartın
    // yanında bulunan Tivibu sayfa düzenleri için kullanılır.
    //
    // Program grupları arasındaki kanal bağlantısı sınırları esas alınır.
    if (rows.size < 10 && channels.length > 0 && programs.length > 0) {
        const channelPositions = channels.map(c => ({
            ...c,
            index: allAnchors.indexOf(c.element)
        }));

        const programPositions = programs.map(p => ({
            ...p,
            index: allAnchors.indexOf(p.element)
        }));

        const ordered = [
            ...channelPositions.map(c => ({
                type: "channel",
                index: c.index,
                data: c
            })),
            ...programPositions.map(p => ({
                type: "program",
                index: p.index,
                data: p
            }))
        ].sort((a, b) => a.index - b.index);

        let currentChannel = null;
        let currentPrograms = [];

        function flushGroup() {
            if (!currentChannel || !currentPrograms.length) return;

            const key = currentChannel.href;

            if (!rows.has(key)) {
                rows.set(key, {
                    name: currentChannel.name,
                    href: key,
                    items: [],
                    seen: new Set()
                });
            }

            const row = rows.get(key);

            for (const item of currentPrograms) {
                if (!row.seen.has(item.text)) {
                    row.seen.add(item.text);
                    row.items.push(item.text);
                }
            }

            currentPrograms = [];
        }

        for (const item of ordered) {
            if (item.type === "channel") {
                flushGroup();
                currentChannel = item.data;
            } else if (currentChannel) {
                currentPrograms.push(item.data);
            }
        }

        flushGroup();
    }

    const output = [];

    for (const row of rows.values()) {
        if (row.items.length) {
            output.push({
                name: row.name,
                href: row.href,
                items: row.items
            });
        }
    }

    const totalMapped = output.reduce(
        (sum, row) => sum + row.items.length,
        0
    );

    return {
        rows: output,
        diagnostics: {
            channelAnchors: channels.length,
            programAnchors: programs.length,
            mappedChannels: output.length,
            mappedPrograms: totalMapped,
            unmappedPrograms: Math.max(0, programs.length - totalMapped),
            samples: programs
                .filter(p => !assignedPrograms.has(p.element))
                .slice(0, 5)
                .map(p => p.text)
        }
    };
}"""


def clean_text(value):
    value = html.unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def slugify(value):
    value = clean_text(value)

    replacements = {
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o",
        "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u",
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
        page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(1200)


def select_date(page, label):
    locator = page.get_by_text(label, exact=True)

    if locator.count() == 0:
        raise RuntimeError(f"Tarih seçeneği bulunamadı: {label}")

    locator.first.click(timeout=15000, force=True)
    page.wait_for_timeout(DATE_WAIT_MS)


def assign_times(items, selected_day):
    parsed = [
        p for p in (parse_program_line(x) for x in items) if p
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
            if p["eh"] * 60 + p["em"] <= start_minutes
            else current_day
        )

        stop = datetime(
            end_day.year, end_day.month, end_day.day,
            p["eh"], p["em"], tzinfo=ISTANBUL
        )

        if stop > start and (stop - start).total_seconds() <= 86400:
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
        today.year, today.month, today.day,
        0, 0, tzinfo=ISTANBUL
    )

    end_day = today + timedelta(days=DAYS)

    window_end = datetime(
        end_day.year, end_day.month, end_day.day,
        0, 0, tzinfo=ISTANBUL
    )

    for category_name, path in CATEGORY_PAGES:
        open_category(page, category_name, path)

        category_channels = set()

        for offset in range(DAYS):
            selected_day = today + timedelta(days=offset)
            label = date_label(selected_day, today)

            print(f"  {label} taranıyor...")

            select_date(page, label)

            extracted = page.evaluate(EXTRACT_JS)
            rows = extracted.get("rows", [])
            diagnostics = extracted.get("diagnostics", {})

            print(
                f"    Kanal bağlantısı: {diagnostics.get('channelAnchors', 0)}"
            )
            print(
                f"    Program bağlantısı: {diagnostics.get('programAnchors', 0)}"
            )
            print(
                f"    Eşleşen kanal: {diagnostics.get('mappedChannels', 0)}"
            )
            print(
                f"    Eşleşen program: {diagnostics.get('mappedPrograms', 0)}"
            )

            if diagnostics.get("samples"):
                print(
                    "    Örnek eşleşmeyen programlar:",
                    " | ".join(diagnostics["samples"][:3])
                )

            if not rows:
                print(
                    f"    UYARI: {category_name} / {label}: "
                    "program grubu bulunamadı."
                )
                continue

            day_program_count = 0

            for row in rows:
                channel_name = clean_text(row.get("name"))

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
                f"    {len(rows)} kanal grubu / "
                f"{len(category_channels)} kategori kanalı / "
                f"{day_program_count} yeni program"
            )

        print(
            f"  {category_name}: {len(category_channels)} kanal bulundu."
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
            root, "channel", {"id": channel_ids[name]}
        )

        display = ET.SubElement(
            channel, "display-name", {"lang": "tr"}
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
                "start": item["start"].strftime("%Y%m%d%H%M%S %z"),
                "stop": item["stop"].strftime("%Y%m%d%H%M%S %z"),
            }
        )

        title = ET.SubElement(
            programme, "title", {"lang": "tr"}
        )
        title.text = item["title"]

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    temp_file = OUTPUT_FILE + ".tmp"

    tree.write(
        temp_file,
        encoding="utf-8",
        xml_declaration=True
    )

    ET.parse(temp_file)
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

    day_count = len({
        p["start"].date() for p in programs
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
    print("TIVIBU 7 GÜNLÜK EPG")
    print("Kategori sayfalarından kanal ve program toplama")
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
            viewport={"width": 1440, "height": 1100}
        )

        page.set_default_timeout(PAGE_TIMEOUT)

        try:
            channel_names, programs = collect_all(page, today)
        finally:
            browser.close()

    validate_data(channel_names, programs)
    build_xml(channel_names, programs)

    days = sorted({
        p["start"].date() for p in programs
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
