
import re
import html
import time
import os
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
DATE_WAIT_MS = 1500
MIN_CHANNELS = 10
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
    "FAVORİ KANALLARIMI",
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


# ============================================================
# TIVIBU DOM EŞLEŞTİRME
#
# Kanal adı yalnızca gerçek /kanallar/ bağlantısından alınır.
# title, aria-label, data-name ve program görseli alt metinleri
# kanal adı olarak kullanılmaz.
#
# Programın üst kapsayıcısında birden fazla farklı kanal varsa
# eşleştirme yapılmaz.
# ============================================================

EXTRACT_JS = r"""() => {
    const norm = s => (s || "").replace(/\s+/g, " ").trim();

    const timeRe =
        /\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}/;

    const allAnchors = [...document.querySelectorAll("a[href]")];

    const programAnchors = allAnchors.filter(a =>
        timeRe.test(norm(a.innerText || a.textContent))
    );

    function channelPath(href) {
        try {
            const u = new URL(href, location.href);

            if (!u.pathname.toLowerCase().includes("/kanallar/")) {
                return "";
            }

            return u.origin + u.pathname.replace(/\/+$/, "");
        } catch (_) {
            return "";
        }
    }

    function cleanChannelName(value) {
        let name = norm(value);

        name = name.replace(/^Kanal\s*:\s*/i, "");
        name = name.replace(/^Kanal\s+/i, "");
        name = norm(name);

        if (!name || name.length > 70) return "";
        if (timeRe.test(name)) return "";

        if (/^(canlı|program akışı|yayın akışı|kanal ara|tüm kanallar)$/i.test(name)) {
            return "";
        }

        return name;
    }

    function getChannelLinks(node) {
        const found = new Map();

        const links = node.querySelectorAll('a[href*="/kanallar/"]');

        for (const a of links) {
            const href = channelPath(a.href);

            if (!href) continue;

            let name = cleanChannelName(
                a.innerText || a.textContent
            );

            if (!name) {
                const img = a.querySelector("img[alt]");
                if (img) {
                    name = cleanChannelName(img.getAttribute("alt"));
                }
            }

            if (!name) continue;

            if (!found.has(href)) {
                found.set(href, {
                    name,
                    href
                });
            }
        }

        return found;
    }

    const groups = new Map();

    const diagnostics = {
        channelLinks: document.querySelectorAll(
            'a[href*="/kanallar/"]'
        ).length,
        programLinks: programAnchors.length,
        mappedPrograms: 0,
        unmappedPrograms: 0,
        mappedChannels: 0,
        samples: []
    };

    for (const programAnchor of programAnchors) {
        const programText = norm(
            programAnchor.innerText || programAnchor.textContent
        );

        let node = programAnchor;
        let match = null;
        let matchDepth = -1;

        // En yakın kapsayıcıdan başlayarak yukarı çık.
        // Çok geniş, bütün sayfayı kapsayan alanları kullanma.
        for (
            let depth = 0;
            depth < 22 && node && node !== document.body;
            depth++, node = node.parentElement
        ) {
            const candidates = getChannelLinks(node);

            if (candidates.size !== 1) {
                continue;
            }

            const programCount = node.querySelectorAll("a[href]").length;

            // Tüm sayfayı veya çok geniş bir listeyi tek kanal
            // altında toplama hatasını engelle.
            if (programCount > 45) {
                continue;
            }

            match = [...candidates.values()][0];
            matchDepth = depth;
            break;
        }

        if (!match) {
            diagnostics.unmappedPrograms++;

            if (diagnostics.samples.length < 12) {
                const parent = programAnchor.parentElement;

                diagnostics.samples.push({
                    program: programText,
                    parent: parent
                        ? norm(parent.innerText || parent.textContent).slice(0, 180)
                        : ""
                });
            }

            continue;
        }

        const key = match.href;

        if (!groups.has(key)) {
            groups.set(key, {
                name: match.name,
                href: match.href,
                items: [],
                seen: new Set(),
                depths: []
            });
        }

        const group = groups.get(key);

        if (!group.seen.has(programText)) {
            group.seen.add(programText);
            group.items.push(programText);
            group.depths.push(matchDepth);
            diagnostics.mappedPrograms++;
        }
    }

    const rows = [];

    for (const group of groups.values()) {
        if (!group.items.length) continue;

        rows.push({
            name: group.name,
            href: group.href,
            items: group.items,
            depth: group.depths
        });
    }

    diagnostics.mappedChannels = rows.length;
    diagnostics.unmappedPrograms =
        programAnchors.length - diagnostics.mappedPrograms;

    return {
        rows,
        diagnostics
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
        value = value.replace(old, new)
        value = value.replace(old.upper(), new)

    value = unicodedata.normalize("NFKD", value)

    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
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

    page.wait_for_timeout(1400)


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

    # Seçilen günün ilk programı seçilen güne aittir.
    # Gece yarısı geçişleri aşağıda otomatik hesaplanır.
    current_day = selected_day

    previous_minutes = None
    result = []

    for p in parsed:
        start_minutes = p["sh"] * 60 + p["sm"]

        # Saat geriye döndüyse yeni güne geç.
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

        end_minutes = p["eh"] * 60 + p["em"]

        end_day = (
            current_day + timedelta(days=1)
            if end_minutes <= start_minutes
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

        duration = (stop - start).total_seconds()

        if 0 < duration <= 86400:
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

            print(
                f"    Kanal bağlantısı: "
                f"{diagnostics.get('channelLinks', 0)}"
            )

            print(
                f"    Program bağlantısı: "
                f"{diagnostics.get('programLinks', 0)}"
            )

            print(
                f"    Eşleşen program: "
                f"{diagnostics.get('mappedPrograms', 0)}"
            )

            print(
                f"    Eşleşen kanal: "
                f"{diagnostics.get('mappedChannels', 0)}"
            )

            print(
                f"    Eşleşmeyen program: "
                f"{diagnostics.get('unmappedPrograms', 0)}"
            )

            for sample in diagnostics.get("samples", [])[:3]:
                print(
                    "    Eşleşmeyen örnek:",
                    sample.get("program", ""),
                    "| kapsayıcı:",
                    sample.get("parent", "")
                )

            if not rows:
                print(
                    f"    UYARI: {label} için "
                    "kanal eşleşmesi bulunamadı."
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
                f"    {len(rows)} grup / "
                f"{len(category_channels)} kanal / "
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

    for name in sorted(
        channel_names,
        key=str.casefold
    ):
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

    for name in sorted(
        channel_names,
        key=str.casefold
    ):
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

    temp_file = OUTPUT_FILE + ".tmp"

    tree.write(
        temp_file,
        encoding="utf-8",
        xml_declaration=True
    )

    # XML dosyasının gerçekten okunabildiğini kontrol et.
    ET.parse(temp_file)

    os.replace(
        temp_file,
        OUTPUT_FILE
    )


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

    # Kanal isimlerinin program başlıklarından oluşmasını engelle.
    invalid_names = [
        name for name in channel_names
        if TIME_RE.search(name)
    ]

    if invalid_names:
        raise RuntimeError(
            "Kanal listesinde yayın saati içeren hatalı "
            f"isim bulundu: {invalid_names[:5]}"
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

    channel_set = set(channel_names)

    invalid_program_channels = [
        p["channel_name"]
        for p in programs
        if p["channel_name"] not in channel_set
    ]

    if invalid_program_channels:
        raise RuntimeError(
            "Kanal listesinde bulunmayan programa rastlandı. "
            "Mevcut XML korunuyor."
        )


def main():
    started = time.time()
    today = datetime.now(ISTANBUL).date()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("Gerçek kanal bağlantısıyla program eşleştirme")
    print(f"Kaynak: {LIVE_TV_URL}")
    print(f"Başlangıç: {today.strftime('%d.%m.%Y')}")
    print(f"Alınacak gün: {DAYS}")
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

        try:
            channel_names, programs = collect_all(
                page,
                today
            )
        finally:
            browser.close()

    validate_data(
        channel_names,
        programs
    )

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
    print(f"İlk tarih: {days[0].strftime('%d.%m.%Y')}")
    print(f"Son tarih: {days[-1].strftime('%d.%m.%Y')}")
    print(f"Dosya: {OUTPUT_FILE}")
    print(f"Süre: {time.time() - started:.1f} saniye")
    print("=" * 68)


if __name__ == "__main__":
    main()
