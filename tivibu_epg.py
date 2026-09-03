import re
import html
from datetime import datetime, timedelta
from xml.etree.ElementTree import Element, SubElement, ElementTree
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


BASE_URL = "https://www.tivibu.com.tr/canli-tv"
OUTPUT_FILE = "epg.xml"

DAYS = 7

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ==============================================================
# YARDIMCI
# ==============================================================

def clean_text(text):
    if not text:
        return ""

    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_name(name):
    return clean_text(name)


def valid_channel_name(name):
    if not name:
        return False

    bad = {
        "NEREDEN NEREYE",
        "COUNT ME IN",
        "SEFİLLER",
        "ÖLÜ MEVSİM",
        "CEBİMDE KELİMELER",
        "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
        "TİVİBU NEDİR?",
        "FAVORİ KANALLARIM",
        "TİVİBU SPOR CANLI İZLE",
        "TRT1 CANLI İZLE",
        "TRT 1 CANLI İZLE",
        "CANLI TV",
        "PROGRAMLAR",
        "KANALLAR",
    }

    if name.upper() in bad:
        return False

    if len(name) > 80:
        return False

    return True


def valid_program_title(title):
    title = clean_text(title)

    if not title:
        return False

    bad = {
        "CANLI TV",
        "KANALLAR",
        "PROGRAMLAR",
        "ANA SAYFA",
        "TİVİBU",
        "TİVİBU CANLI TV",
        "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
        "TİVİBU NEDİR?",
        "FAVORİ KANALLARIM",
        "TRT1 CANLI İZLE",
        "TRT 1 CANLI İZLE",
    }

    if title.upper() in bad:
        return False

    if len(title) > 250:
        return False

    return True


def extract_channel_id(href):
    if not href:
        return None

    match = re.search(r"(ch[a-zA-Z0-9]+)", href)

    if match:
        return match.group(1)

    return None


def parse_time(text):
    if not text:
        return None

    match = re.search(
        r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b",
        text
    )

    if not match:
        return None

    return int(match.group(1)), int(match.group(2))


def make_datetime(date_obj, hour, minute):
    return datetime(
        date_obj.year,
        date_obj.month,
        date_obj.day,
        hour,
        minute,
    )


def xml_datetime(dt):
    return dt.strftime("%Y%m%d%H%M%S") + " +0300"


def channel_xml_id(name):
    value = name.lower()

    replacements = {
        "ı": "i",
        "İ": "i",
        "ş": "s",
        "Ş": "s",
        "ğ": "g",
        "Ğ": "g",
        "ü": "u",
        "Ü": "u",
        "ö": "o",
        "Ö": "o",
        "ç": "c",
        "Ç": "c",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    value = re.sub(r"[^a-zA-Z0-9]+", "_", value)
    value = value.strip("_")

    return value


# ==============================================================
# KANALLARI BUL
# ==============================================================

def collect_channels(page):
    channels = []
    seen = set()

    locator = page.locator('a[href*="/kanallar/"]')

    count = locator.count()

    for i in range(count):
        try:
            element = locator.nth(i)

            href = element.get_attribute("href")
            text = clean_text(element.inner_text())

            if not href or not text:
                continue

            name = normalize_name(text)

            if not valid_channel_name(name):
                continue

            if name.upper() in seen:
                continue

            if href.startswith("/"):
                href = "https://www.tivibu.com.tr" + href

            seen.add(name.upper())

            channels.append({
                "name": name,
                "url": href,
                "id": channel_xml_id(name),
                "source_id": None,
            })

        except Exception:
            continue

    return channels


# ==============================================================
# PROGRAM ID'LERİNİ SAYFADAKİ SIRAYLA BUL
# ==============================================================

def collect_program_channel_ids(page):
    ids = []
    seen = set()

    locator = page.locator(
        'a[href*="/rv?"][href*="ch"]'
    )

    count = locator.count()

    for i in range(count):
        try:
            href = locator.nth(i).get_attribute("href")

            channel_id = extract_channel_id(href)

            if not channel_id:
                continue

            if channel_id in seen:
                continue

            seen.add(channel_id)
            ids.append(channel_id)

        except Exception:
            continue

    return ids


# ==============================================================
# KANAL ID -> KANAL ADI
#
# ÖNEMLİ:
# Tivibu sayfasında kanal linklerinin parent elementlerinde
# bulunan ch ID güvenilir değil.
#
# Program linkleri ise kanal bazında gruplanmış durumda.
# Bu nedenle unique ch ID sırası ile kanal sırası eşleştiriliyor.
# ==============================================================

def build_channel_map(page, channels):
    program_ids = collect_program_channel_ids(page)

    print()
    print("Program kanal ID'leri:")

    for index, channel_id in enumerate(program_ids, 1):
        print(f"  {index}. {channel_id}")

    channel_map = {}

    usable_channels = []

    for channel in channels:
        name = channel["name"]

        if not valid_channel_name(name):
            continue

        usable_channels.append(channel)

    # Tivibu Tanıtım dahil gerçek kanal sırası korunuyor.
    # Program grupları da aynı sırada geliyor.

    limit = min(
        len(program_ids),
        len(usable_channels)
    )

    for i in range(limit):
        source_id = program_ids[i]
        channel = usable_channels[i]

        channel["source_id"] = source_id

        channel_map[source_id] = channel["name"]

        print(
            f"  Eşleşme: {source_id} -> {channel['name']}"
        )

    return channel_map


# ==============================================================
# TARİH SEÇ
# ==============================================================

def select_date(page, target_date):
    date_variants = [
        target_date.strftime("%d.%m.%Y"),
        target_date.strftime("%d/%m/%Y"),
        target_date.strftime("%Y-%m-%d"),
        target_date.strftime("%d.%m"),
    ]

    # Önce button / role button
    for value in date_variants:
        try:
            locator = page.locator(
                "button, [role='button']"
            ).filter(
                has_text=value
            )

            if locator.count() > 0:
                locator.first.click(timeout=2500)
                page.wait_for_timeout(700)
                return True

        except Exception:
            pass

    # Sonra genel text
    for value in date_variants:
        try:
            locator = page.get_by_text(
                value,
                exact=True
            )

            if locator.count() > 0:
                locator.first.click(timeout=2500)
                page.wait_for_timeout(700)
                return True

        except Exception:
            pass

    # JS fallback
    try:
        result = page.evaluate(
            """
            (dates) => {
                const elements = [
                    ...document.querySelectorAll("button"),
                    ...document.querySelectorAll("[role='button']"),
                    ...document.querySelectorAll("a")
                ];

                for (const el of elements) {
                    const text = (el.innerText || "").trim();

                    if (dates.includes(text)) {
                        el.click();
                        return true;
                    }
                }

                return false;
            }
            """,
            date_variants
        )

        if result:
            page.wait_for_timeout(700)
            return True

    except Exception:
        pass

    return False


# ==============================================================
# PROGRAM BAŞLIĞI + SAATİ
# ==============================================================

def extract_program_data(element):
    try:
        title = clean_text(element.inner_text())
    except Exception:
        return None, None

    if not title:
        return None, None

    # Önce linkin kendi metni
    time_info = parse_time(title)

    # Linkte saat yoksa parent'lardan ara
    if not time_info:
        try:
            parent_text = element.evaluate(
                """
                el => {
                    let node = el;

                    for (let i = 0; i < 6 && node; i++) {
                        const text = node.innerText || "";

                        if (/\\b([01]?\\d|2[0-3])[:.]([0-5]\\d)\\b/.test(text)) {
                            return text;
                        }

                        node = node.parentElement;
                    }

                    return "";
                }
                """
            )

            time_info = parse_time(parent_text)

        except Exception:
            pass

    if not time_info:
        return None, None

    hour, minute = time_info

    # Saat bilgisini başlıktan temizle
    title = re.sub(
        r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b",
        "",
        title
    )

    title = re.sub(
        r"\s+",
        " ",
        title
    ).strip()

    if not valid_program_title(title):
        return None, None

    return title, (hour, minute)


# ==============================================================
# PROGRAMLARI ÇEK
# ==============================================================

def collect_programs(page, channel_map, target_date):
    programs = []
    seen = set()

    locator = page.locator(
        'a[href*="/rv?"][href*="ch"]'
    )

    count = locator.count()

    print(
        f"Program bağlantısı: {count}"
    )

    for i in range(count):
        try:
            element = locator.nth(i)

            href = element.get_attribute("href")

            source_id = extract_channel_id(href)

            if not source_id:
                continue

            channel_name = channel_map.get(source_id)

            if not channel_name:
                continue

            title, time_info = extract_program_data(
                element
            )

            if not title or not time_info:
                continue

            hour, minute = time_info

            start = make_datetime(
                target_date,
                hour,
                minute
            )

            key = (
                channel_name,
                start,
                title
            )

            if key in seen:
                continue

            seen.add(key)

            programs.append({
                "channel": channel_name,
                "start": start,
                "title": title,
            })

        except Exception:
            continue

    programs.sort(
        key=lambda x: (
            x["start"],
            x["channel"],
            x["title"]
        )
    )

    return programs


# ==============================================================
# TARİHLER ARASINDA GEÇİŞTE PROGRAMLARIN YENİLENMESİNİ BEKLE
# ==============================================================

def wait_for_program_refresh(page, old_count):
    try:
        page.wait_for_function(
            """
            oldCount => {
                const count = document.querySelectorAll(
                    'a[href*="/rv?"][href*="ch"]'
                ).length;

                return count !== oldCount && count > 0;
            }
            """,
            old_count,
            timeout=5000
        )

    except Exception:
        pass

    page.wait_for_timeout(500)


# ==============================================================
# BİTİŞ SAATLERİ
# ==============================================================

def fix_end_times(programs):
    grouped = {}

    for program in programs:
        grouped.setdefault(
            program["channel"],
            []
        ).append(program)

    result = []

    for channel_name, items in grouped.items():

        items.sort(
            key=lambda x: x["start"]
        )

        for index, program in enumerate(items):

            start = program["start"]

            if index + 1 < len(items):

                end = items[index + 1]["start"]

                # Gece yarısını doğru ele al
                if end <= start:
                    end = start + timedelta(
                        minutes=30
                    )

            else:
                end = start + timedelta(
                    minutes=30
                )

            # Aşırı uzun programları sınırla
            if end - start > timedelta(hours=12):
                end = start + timedelta(
                    minutes=180
                )

            result.append({
                **program,
                "end": end,
            })

    result.sort(
        key=lambda x: (
            x["start"],
            x["channel"]
        )
    )

    return result


# ==============================================================
# XML
# ==============================================================

def create_xml(channels, programs):

    tv = Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": "https://www.tivibu.com.tr/",
        }
    )

    # ----------------------------------------------------------
    # KANALLAR
    # ----------------------------------------------------------

    used_channel_names = {
        program["channel"]
        for program in programs
    }

    final_channels = []

    for channel in channels:

        if channel["name"] not in used_channel_names:
            continue

        final_channels.append(channel)

        channel_element = SubElement(
            tv,
            "channel",
            {
                "id": channel["id"]
            }
        )

        display_name = SubElement(
            channel_element,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display_name.text = channel["name"]

    # ----------------------------------------------------------
    # PROGRAMLAR
    # ----------------------------------------------------------

    channel_ids = {
        channel["name"]: channel["id"]
        for channel in final_channels
    }

    for program in programs:

        channel_name = program["channel"]

        channel_id = channel_ids.get(
            channel_name
        )

        if not channel_id:
            continue

        programme = SubElement(
            tv,
            "programme",
            {
                "channel": channel_id,
                "start": xml_datetime(
                    program["start"]
                ),
                "stop": xml_datetime(
                    program["end"]
                ),
            }
        )

        title = SubElement(
            programme,
            "title",
            {
                "lang": "tr"
            }
        )

        title.text = program["title"]

    # ----------------------------------------------------------
    # YAZ
    # ----------------------------------------------------------

    try:
        import xml.etree.ElementTree as ET
        ET.indent(
            ElementTree(tv),
            space="  "
        )
    except Exception:
        pass

    tree = ElementTree(tv)

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True
    )

    return len(final_channels)


# ==============================================================
# ANA
# ==============================================================

def main():

    today = datetime.now().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    end_date = today + timedelta(
        days=DAYS - 1
    )

    print("=" * 70)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("=" * 70)

    print(
        f"Dönem: "
        f"{today.strftime('%d.%m.%Y')} -> "
        f"{end_date.strftime('%d.%m.%Y')}"
    )

    print("=" * 70)

    all_programs = []

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1920,
                "height": 1080
            }
        )

        page = context.new_page()

        page.set_default_timeout(
            8000
        )

        print("Tivibu açılıyor...")

        try:
            page.goto(
                BASE_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

        except PlaywrightTimeoutError:
            print(
                "Sayfa yükleme zaman aşımı."
            )

        page.wait_for_timeout(2500)

        # ------------------------------------------------------
        # KANALLAR
        # ------------------------------------------------------

        channels = collect_channels(page)

        print()
        print(
            f"Bulunan kanal: {len(channels)}"
        )

        for index, channel in enumerate(
            channels,
            1
        ):
            print(
                f"  {index}. {channel['name']}"
            )

        # ------------------------------------------------------
        # KANAL ID EŞLEŞMESİ
        # ------------------------------------------------------

        print()
        print(
            "Kanal-program eşleşmesi oluşturuluyor..."
        )

        channel_map = build_channel_map(
            page,
            channels
        )

        if not channel_map:
            print(
                "HATA: Program kanal ID'leri bulunamadı."
            )

            browser.close()
            return

        print()
        print(
            f"Eşleşen kanal: {len(channel_map)}"
        )

        # ------------------------------------------------------
        # 7 GÜN
        # ------------------------------------------------------

        for day_index in range(DAYS):

            target_date = today + timedelta(
                days=day_index
            )

            print()
            print("=" * 70)
            print(
                f"Tarih: "
                f"{target_date.strftime('%d.%m.%Y')}"
            )
            print("=" * 70)

            if day_index > 0:

                old_count = page.locator(
                    'a[href*="/rv?"][href*="ch"]'
                ).count()

                selected = select_date(
                    page,
                    target_date
                )

                if not selected:

                    print(
                        "Tarih seçilemedi, sayfa yenileniyor..."
                    )

                    try:
                        page.reload(
                            wait_until="domcontentloaded",
                            timeout=60000
                        )

                        page.wait_for_timeout(
                            1800
                        )

                        # Sayfa yenilenince kanal ID eşleşmesini
                        # tekrar kur.
                        channel_map = build_channel_map(
                            page,
                            channels
                        )

                    except Exception:
                        pass

                else:
                    wait_for_program_refresh(
                        page,
                        old_count
                    )

            programs = collect_programs(
                page,
                channel_map,
                target_date
            )

            print(
                f"Bulunan program: {len(programs)}"
            )

            day_channel_count = {}

            for program in programs:

                name = program["channel"]

                day_channel_count[name] = (
                    day_channel_count.get(
                        name,
                        0
                    ) + 1
                )

            for channel_name, count in sorted(
                day_channel_count.items()
            ):
                print(
                    f"  {channel_name}: "
                    f"{count} program"
                )

            all_programs.extend(
                programs
            )

        browser.close()

    # ==========================================================
    # DUPLICATE TEMİZLE
    # ==========================================================

    unique = []
    seen = set()

    for program in all_programs:

        key = (
            program["channel"],
            program["start"],
            program["title"]
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(program)

    # ==========================================================
    # BİTİŞ SAATLERİ
    # ==========================================================

    unique = fix_end_times(
        unique
    )

    # ==========================================================
    # SADECE PROGRAMI OLAN KANALLAR
    # ==========================================================

    used_channels = {
        program["channel"]
        for program in unique
    }

    final_channels = [
        channel
        for channel in channels
        if channel["name"] in used_channels
    ]

    # ==========================================================
    # XML
    # ==========================================================

    channel_count = create_xml(
        final_channels,
        unique
    )

    # ==========================================================
    # SONUÇ
    # ==========================================================

    print()
    print("=" * 70)
    print("SONUÇ")
    print("=" * 70)

    print(
        f"Kanal: {channel_count}"
    )

    print(
        f"Toplam program: {len(unique)}"
    )

    print("=" * 70)

    print(
        f"{OUTPUT_FILE} oluşturuldu."
    )


if __name__ == "__main__":
    main()
