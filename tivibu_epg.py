import re
import time
import html
from datetime import datetime, timedelta
from xml.etree.ElementTree import Element, SubElement, ElementTree
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


BASE_URL = "https://www.tivibu.com.tr/canli-tv"
OUTPUT_FILE = "epg.xml"

DAYS = 7

TURKEY_TZ = "+0300"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ----------------------------------------------------------------------
# YARDIMCI FONKSİYONLAR
# ----------------------------------------------------------------------

def clean_text(text):
    if not text:
        return ""

    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_name(name):
    name = clean_text(name)

    replacements = {
        "TİVİBU TANITIM": "Tivibu Tanıtım",
        "TİVİBU SPOR": "Tivibu Spor",
        "TİVİBU SPOR 1": "Tivibu Spor 1",
        "TİVİBU SPOR 2": "Tivibu Spor 2",
        "TİVİBU SPOR 3": "Tivibu Spor 3",
        "TİVİBU SPOR 4": "Tivibu Spor 4",
    }

    return replacements.get(name.upper(), name)


def valid_channel_name(name):
    if not name:
        return False

    bad_names = {
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

    if name.upper() in bad_names:
        return False

    if len(name) > 80:
        return False

    return True


def valid_program_title(title):
    title = clean_text(title)

    if not title:
        return False

    bad_titles = {
        "CANLI TV",
        "KANALLAR",
        "PROGRAMLAR",
        "TİVİBU",
        "ANA SAYFA",
        "TİVİBU CANLI TV",
        "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
        "TİVİBU NEDİR?",
        "FAVORİ KANALLARIM",
        "TRT1 CANLI İZLE",
        "TRT1 CANLI İZLE - TRT 1",
    }

    if title.upper() in bad_titles:
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


def parse_program_url(href):
    if not href:
        return None

    return extract_channel_id(href)


def parse_time_from_text(text):
    """
    Program kartındaki başlangıç saatini bulur.
    Örnek:
    21:30
    21.30
    21:30 - 22:30
    """

    if not text:
        return None

    patterns = [
        r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))

            return hour, minute

    return None


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


# ----------------------------------------------------------------------
# KANALLARI BUL
# ----------------------------------------------------------------------

def collect_channels(page):
    channels = []

    locator = page.locator('a[href*="/kanallar/"]')

    count = locator.count()

    seen = set()

    for i in range(count):
        try:
            element = locator.nth(i)

            href = element.get_attribute("href")
            text = clean_text(element.inner_text())

            if not href:
                continue

            if not text:
                continue

            name = normalize_name(text)

            if not valid_channel_name(name):
                continue

            if name in seen:
                continue

            if href.startswith("/"):
                href = "https://www.tivibu.com.tr" + href

            seen.add(name)

            channels.append({
                "name": name,
                "url": href,
                "id": None,
            })

        except Exception:
            continue

    return channels


# ----------------------------------------------------------------------
# KANAL ID'SİNİ TEK SAYFADAN BUL
# ----------------------------------------------------------------------

def find_id_in_ancestor(element):
    """
    Kanal linkinin bulunduğu DOM ağacında chxxxxxxxx ID'sini arar.

    Her kanal için yeni sayfa açılmaz.
    """

    try:
        return element.evaluate(
            """
            (el) => {
                let node = el;

                for (let i = 0; i < 12 && node; i++) {
                    const html = node.outerHTML || "";

                    const matches = html.match(/\\bch[a-zA-Z0-9]+\\b/g) || [];
                    const unique = [...new Set(matches)];

                    if (unique.length === 1) {
                        return unique[0];
                    }

                    node = node.parentElement;
                }

                return null;
            }
            """
        )

    except Exception:
        return None


def build_channel_id_map(page, channels):
    """
    Tek canlı-tv sayfasındaki DOM üzerinden kanal -> chID eşleşmesi.

    Öncelik:
    1. Kanal kartının DOM ağacındaki chID
    2. Kanalın href'i
    """

    channel_map = {}

    channel_locator = page.locator('a[href*="/kanallar/"]')

    count = channel_locator.count()

    for i in range(count):
        try:
            element = channel_locator.nth(i)

            text = clean_text(element.inner_text())

            if not text:
                continue

            name = normalize_name(text)

            if not valid_channel_name(name):
                continue

            channel_id = find_id_in_ancestor(element)

            if channel_id:
                channel_map[channel_id] = name

        except Exception:
            continue

    # --------------------------------------------------------------
    # Eğer DOM'dan bazı ID'ler bulunamadıysa program gruplarından
    # kalan kanal ID'lerini çıkar.
    # --------------------------------------------------------------

    program_locator = page.locator(
        'a[href*="/rv?"][href*="ch"]'
    )

    program_count = program_locator.count()

    program_ids = []

    for i in range(program_count):
        try:
            href = program_locator.nth(i).get_attribute("href")

            channel_id = parse_program_url(href)

            if channel_id and channel_id not in program_ids:
                program_ids.append(channel_id)

        except Exception:
            continue

    # --------------------------------------------------------------
    # DOM kartlarında ID bulunamadıysa kanal sırasını kullan.
    # Tivibu sayfasında program grupları kanal sırasıyla gelir.
    # --------------------------------------------------------------

    if len(channel_map) < 3 and program_ids:
        usable_channels = [
            channel["name"]
            for channel in channels
            if channel["name"].upper() != "BENİM KANALIM"
        ]

        for channel_id, channel_name in zip(
            program_ids,
            usable_channels
        ):
            if channel_id not in channel_map:
                channel_map[channel_id] = channel_name

    return channel_map


# ----------------------------------------------------------------------
# PROGRAMLARI TEK SAYFADAN ÇIKAR
# ----------------------------------------------------------------------

def collect_programs(page, channel_map, target_date):
    programs = []

    locator = page.locator(
        'a[href*="/rv?"][href*="ch"]'
    )

    count = locator.count()

    seen = set()

    for i in range(count):
        try:
            element = locator.nth(i)

            href = element.get_attribute("href")

            channel_id = parse_program_url(href)

            if not channel_id:
                continue

            channel_name = channel_map.get(channel_id)

            if not channel_name:
                continue

            title = clean_text(element.inner_text())

            if not valid_program_title(title):
                continue

            time_info = parse_time_from_text(title)

            if time_info:
                hour, minute = time_info

                # Saat bilgisini başlıktan kaldır.
                title = re.sub(
                    r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b",
                    "",
                    title,
                )

                title = re.sub(r"\s+", " ", title).strip()

            else:
                # Program kartında saat yoksa aynı kartın parent
                # alanından saat bulmayı dene.
                try:
                    parent_text = element.evaluate(
                        """
                        el => {
                            let n = el;

                            for (let i = 0; i < 5 && n; i++) {
                                const t = n.innerText || "";
                                if (/\\b([01]?\\d|2[0-3])[:.]([0-5]\\d)\\b/.test(t)) {
                                    return t;
                                }
                                n = n.parentElement;
                            }

                            return "";
                        }
                        """
                    )

                    time_info = parse_time_from_text(parent_text)

                except Exception:
                    time_info = None

            if not time_info:
                continue

            hour, minute = time_info

            title = clean_text(title)

            if not valid_program_title(title):
                continue

            if len(title) < 2:
                continue

            start = make_datetime(
                target_date,
                hour,
                minute,
            )

            key = (
                channel_name,
                start.strftime("%Y-%m-%d %H:%M"),
                title,
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
            x["channel"],
            x["start"],
            x["title"],
        )
    )

    return programs


# ----------------------------------------------------------------------
# PROGRAM SAATLERİNİ DÜZELT
# ----------------------------------------------------------------------

def fix_program_end_times(programs):
    """
    XMLTV end zamanı için aynı kanaldaki bir sonraki programın
    başlangıcını kullanır.

    Son programın bitişi 24 saat sınırına göre hesaplanır.
    """

    grouped = {}

    for program in programs:
        grouped.setdefault(
            program["channel"],
            []
        ).append(program)

    result = []

    for channel, channel_programs in grouped.items():

        channel_programs.sort(
            key=lambda x: x["start"]
        )

        for index, program in enumerate(channel_programs):

            start = program["start"]

            if index + 1 < len(channel_programs):
                end = channel_programs[index + 1]["start"]

                if end <= start:
                    end = start + timedelta(minutes=30)

            else:
                end = start + timedelta(minutes=30)

            item = dict(program)
            item["end"] = end

            result.append(item)

    return result


# ----------------------------------------------------------------------
# XML OLUŞTUR
# ----------------------------------------------------------------------

def create_xml(channels, programs):
    tv = Element(
        "tv",
        {
            "generator-info-name": "Tivibu XMLTV EPG",
            "generator-info-url": "https://www.tivibu.com.tr/",
        },
    )

    channel_names = set()

    for channel in channels:

        name = normalize_name(channel["name"])

        if not valid_channel_name(name):
            continue

        if name in channel_names:
            continue

        channel_names.add(name)

        channel_id = (
            channel["id"]
            if channel.get("id")
            else re.sub(
                r"[^a-zA-Z0-9]+",
                "_",
                name.lower()
            ).strip("_")
        )

        channel_element = SubElement(
            tv,
            "channel",
            {
                "id": channel_id,
            },
        )

        display_name = SubElement(
            channel_element,
            "display-name",
            {
                "lang": "tr",
            },
        )

        display_name.text = name

    # --------------------------------------------------------------
    # XML programları
    # --------------------------------------------------------------

    for program in sorted(
        programs,
        key=lambda x: (
            x["start"],
            x["channel"],
        )
    ):

        channel_name = program["channel"]

        channel_id = None

        for channel in channels:
            if channel["name"] == channel_name:
                channel_id = channel.get("id")
                break

        if not channel_id:
            channel_id = re.sub(
                r"[^a-zA-Z0-9]+",
                "_",
                channel_name.lower()
            ).strip("_")

        programme = SubElement(
            tv,
            "programme",
            {
                "channel": channel_id,
                "start": xml_datetime(program["start"]),
                "stop": xml_datetime(program["end"]),
            },
        )

        title = SubElement(
            programme,
            "title",
            {
                "lang": "tr",
            },
        )

        title.text = program["title"]

    tree = ElementTree(tv)

    try:
        import xml.etree.ElementTree as ET

        ET.indent(tree, space="  ")

    except Exception:
        pass

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )


# ----------------------------------------------------------------------
# TARİH SEÇİMİ
# ----------------------------------------------------------------------

def select_date(page, target_date):
    """
    Tivibu canlı TV sayfasındaki tarih seçiciyi kullanır.

    Önce YYYY-MM-DD,
    sonra DD.MM.YYYY,
    sonra DD/MM/YYYY biçimlerini dener.
    """

    date_strings = [
        target_date.strftime("%Y-%m-%d"),
        target_date.strftime("%d.%m.%Y"),
        target_date.strftime("%d/%m/%Y"),
        target_date.strftime("%d.%m"),
    ]

    for date_string in date_strings:

        try:
            locator = page.get_by_text(
                date_string,
                exact=True
            )

            if locator.count() > 0:

                locator.first.click(
                    timeout=3000
                )

                page.wait_for_timeout(700)

                return True

        except Exception:
            pass

    # --------------------------------------------------------------
    # Tarih butonlarını JS üzerinden dene.
    # --------------------------------------------------------------

    try:
        result = page.evaluate(
            """
            (dateStrings) => {

                const elements = [
                    ...document.querySelectorAll("button"),
                    ...document.querySelectorAll("[role='button']"),
                    ...document.querySelectorAll("a")
                ];

                for (const el of elements) {

                    const text = (el.innerText || "").trim();

                    if (dateStrings.includes(text)) {
                        el.click();
                        return true;
                    }
                }

                return false;
            }
            """,
            date_strings,
        )

        if result:
            page.wait_for_timeout(700)
            return True

    except Exception:
        pass

    return False


# ----------------------------------------------------------------------
# ANA PROGRAM
# ----------------------------------------------------------------------

def main():

    print("=" * 70)
    print("TIVIBU EPG")
    print("=" * 70)

    start_date = datetime.now()

    print(
        f"Dönem: {start_date.strftime('%d.%m.%Y')} -> "
        f"{(start_date + timedelta(days=DAYS - 1)).strftime('%d.%m.%Y')}"
    )

    print("=" * 70)

    all_programs = []

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1920,
                "height": 1080,
            },
        )

        page = context.new_page()

        page.set_default_timeout(10000)

        print("Tivibu açılıyor...")

        try:
            page.goto(
                BASE_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

        except PlaywrightTimeoutError:
            print("Sayfa yükleme zaman aşımı, mevcut DOM kullanılacak.")

        page.wait_for_timeout(2500)

        # ----------------------------------------------------------
        # KANALLAR
        # ----------------------------------------------------------

        channels = collect_channels(page)

        print(
            f"Bulunan kanal: {len(channels)}"
        )

        for channel in channels:
            print(
                f"  {channel['name']}"
            )

        # ----------------------------------------------------------
        # KANAL ID'LERİ
        # ----------------------------------------------------------

        print()
        print("Kanal ID'leri çıkarılıyor...")

        channel_map = build_channel_id_map(
            page,
            channels,
        )

        # ----------------------------------------------------------
        # KANAL ID -> KANAL ADI
        # ----------------------------------------------------------

        reverse_map = {}

        for channel_id, channel_name in channel_map.items():

            reverse_map[channel_id] = channel_name

            print(
                f"  {channel_name}: {channel_id}"
            )

        # ----------------------------------------------------------
        # KANALLARA ID ATA
        # ----------------------------------------------------------

        for channel in channels:

            channel_name = channel["name"]

            found_id = None

            for channel_id, name in channel_map.items():

                if name == channel_name:
                    found_id = channel_id
                    break

            if found_id:
                channel["id"] = channel_name.lower()

                channel["epg_id"] = found_id

        # ----------------------------------------------------------
        # XML ID'LERİNİ STANDARTLAŞTIR
        # ----------------------------------------------------------

        for channel in channels:

            channel["id"] = re.sub(
                r"[^a-zA-Z0-9]+",
                "_",
                channel["name"].lower()
            ).strip("_")

        # ----------------------------------------------------------
        # 7 GÜN
        # ----------------------------------------------------------

        for day_index in range(DAYS):

            target_date = (
                start_date + timedelta(days=day_index)
            )

            print("=" * 70)

            print(
                f"Tarih: {target_date.strftime('%d.%m.%Y')}"
            )

            print("=" * 70)

            if day_index > 0:

                selected = select_date(
                    page,
                    target_date,
                )

                if not selected:

                    print(
                        "Tarih seçici bulunamadı; "
                        "sayfa yeniden yükleniyor..."
                    )

                    try:
                        page.reload(
                            wait_until="domcontentloaded",
                            timeout=60000,
                        )

                        page.wait_for_timeout(1800)

                    except Exception:
                        pass

            # ------------------------------------------------------
            # PROGRAMLARI ÇEK
            # ------------------------------------------------------

            programs = collect_programs(
                page,
                reverse_map,
                target_date,
            )

            print(
                f"Bulunan program: {len(programs)}"
            )

            # ------------------------------------------------------
            # Kanal bazında göster
            # ------------------------------------------------------

            day_channels = {}

            for program in programs:

                day_channels.setdefault(
                    program["channel"],
                    0,
                )

                day_channels[program["channel"]] += 1

            for channel_name, count in sorted(
                day_channels.items()
            ):
                print(
                    f"  {channel_name}: {count} program"
                )

            all_programs.extend(programs)

            print(
                f"{target_date.strftime('%d.%m.%Y')}: "
                f"{len(programs)} program eklendi"
            )

            # ------------------------------------------------------
            # Tarih değişiminin tamamlanmasını bekle
            # ------------------------------------------------------

            page.wait_for_timeout(400)

        browser.close()

    # ------------------------------------------------------------------
    # DUPLICATE TEMİZLE
    # ------------------------------------------------------------------

    unique_programs = []

    seen = set()

    for program in all_programs:

        key = (
            program["channel"],
            program["start"],
            program["title"],
        )

        if key in seen:
            continue

        seen.add(key)

        unique_programs.append(program)

    # ------------------------------------------------------------------
    # SAATLERİ DÜZELT
    # ------------------------------------------------------------------

    unique_programs = fix_program_end_times(
        unique_programs
    )

    # ------------------------------------------------------------------
    # SADECE PROGRAMI BULUNAN KANALLARI KORU
    # ------------------------------------------------------------------

    used_channels = {
        program["channel"]
        for program in unique_programs
    }

    final_channels = []

    for channel in channels:

        if channel["name"] in used_channels:

            final_channels.append(channel)

    # ------------------------------------------------------------------
    # XML
    # ------------------------------------------------------------------

    create_xml(
        final_channels,
        unique_programs,
    )

    # ------------------------------------------------------------------
    # SONUÇ
    # ------------------------------------------------------------------

    print()
    print("=" * 70)
    print("SONUÇ")
    print("=" * 70)

    print(
        f"Kanal: {len(final_channels)}"
    )

    print(
        f"Toplam program: {len(unique_programs)}"
    )

    print("=" * 70)

    print(
        f"{OUTPUT_FILE} oluşturuldu."
    )


if __name__ == "__main__":
    main()
