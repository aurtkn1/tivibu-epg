import re
import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from xml.etree.ElementTree import Element, SubElement, ElementTree
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


OUTPUT_FILE = "epg.xml"
DAYS = 7

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

BASE = "https://www.tivibu.com.tr"

CATEGORY_URLS = [
    f"{BASE}/canli-tv",
    f"{BASE}/canli-tv/ulusal",
    f"{BASE}/canli-tv/muzik",
    f"{BASE}/canli-tv/yasam-stil",
    f"{BASE}/canli-tv/dizi",
    f"{BASE}/canli-tv/spor",
    f"{BASE}/canli-tv/haber",
    f"{BASE}/canli-tv/belgesel",
    f"{BASE}/canli-tv/cocuk",
    f"{BASE}/canli-tv/sinema",
    f"{BASE}/canli-tv/global",
    f"{BASE}/canli-tv/diger",
]

TURKEY_TZ = ZoneInfo("Europe/Istanbul")


# ==============================================================
# TEMEL YARDIMCILAR
# ==============================================================

def clean_text(text):
    if not text:
        return ""

    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_name(name):
    return clean_text(name)


def valid_channel_name(name):
    if not name:
        return False

    upper = name.upper()

    bad_names = {
        "BENİM KANALIM",
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
        "KANAL ARA",
        "BUGÜN",
        "DÜN",
        "YARIN",
    }

    if upper in bad_names:
        return False

    if len(name) > 80:
        return False

    return True


def valid_program_title(title):
    title = clean_text(title)

    if not title:
        return False

    if len(title) > 250:
        return False

    bad_titles = {
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

    if title.upper() in bad_titles:
        return False

    return True


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


def extract_channel_id(href):
    if not href:
        return None

    match = re.search(
        r"\b(ch[a-zA-Z0-9]+)\b",
        href
    )

    if match:
        return match.group(1)

    return None


def xml_datetime(dt):
    return dt.strftime("%Y%m%d%H%M%S") + " +0300"


def make_datetime(date_obj, hour, minute):
    return datetime(
        date_obj.year,
        date_obj.month,
        date_obj.day,
        hour,
        minute,
        tzinfo=TURKEY_TZ,
    )


# ==============================================================
# PROGRAM BAŞLIĞINI AYRIŞTIR
# ==============================================================

PROGRAM_CATEGORIES = (
    "Film",
    "Dizi",
    "Yaşam",
    "Spor Programı",
    "Spor",
    "Haber",
    "Belgesel",
    "Çocuk",
    "Müzik",
    "Eğlence",
    "Aktüalite",
    "Diğer",
)


def parse_program_text(text):
    """
    Tivibu metinleri şu yapıda geliyor:

    Program Adı Film - 23:30 → 01:15 Canlı

    veya

    Program Adı Yaşam - 23:55 → 00:35 Canlı

    Buradan başlık + başlangıç + bitiş çıkarılır.
    """

    text = clean_text(text)

    if not text:
        return None

    pattern = re.compile(
        r"^(?P<title>.+?)"
        r"\s+(?P<category>"
        + "|".join(re.escape(x) for x in PROGRAM_CATEGORIES)
        + r")"
        r"\s*-\s*"
        r"(?P<start>\d{1,2}[:.]\d{2})"
        r"\s*→\s*"
        r"(?P<end>\d{1,2}[:.]\d{2})"
        r"(?:\s*Canlı)?$",
        re.IGNORECASE,
    )

    match = pattern.match(text)

    if match:
        title = clean_text(match.group("title"))

        start_text = match.group("start").replace(".", ":")
        end_text = match.group("end").replace(".", ":")

        sh, sm = start_text.split(":")
        eh, em = end_text.split(":")

        return {
            "title": title,
            "start_hour": int(sh),
            "start_minute": int(sm),
            "end_hour": int(eh),
            "end_minute": int(em),
        }

    # Kategori bulunamazsa sadece saat aralığını kaldır.
    fallback = re.match(
        r"^(.*?)\s*-\s*"
        r"\d{1,2}[:.]\d{2}"
        r"\s*→\s*"
        r"\d{1,2}[:.]\d{2}"
        r"(?:\s*Canlı)?$",
        text,
        re.IGNORECASE,
    )

    if fallback:
        title = clean_text(fallback.group(1))

        if valid_program_title(title):
            return {
                "title": title,
                "start_hour": None,
                "start_minute": None,
                "end_hour": None,
                "end_minute": None,
            }

    return None


# ==============================================================
# KANALLARI TOPLA
# ==============================================================

def collect_channels_from_page(page):
    channels = []
    seen = set()

    locator = page.locator(
        'a[href*="/kanallar/"]'
    )

    count = locator.count()

    for i in range(count):
        try:
            element = locator.nth(i)

            href = element.get_attribute("href")
            text = clean_text(
                element.inner_text()
            )

            if not href or not text:
                continue

            name = normalize_name(text)

            if not valid_channel_name(name):
                continue

            key = name.upper()

            if key in seen:
                continue

            if href.startswith("/"):
                href = BASE + href

            seen.add(key)

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
# PROGRAM KANAL ID'LERİNİ DOM SIRASIYLA TOPLA
# ==============================================================

def collect_program_groups(page):
    """
    Her kanalın program linkleri aynı chXXXX ID'sini kullanıyor.

    Örnek:
      ch00001 -> kanal 1'in tüm programları
      ch00002 -> kanal 2'nin tüm programları

    İlk görülme sırasını koruyoruz.
    """

    locator = page.locator(
        'a[href*="/rv?"][href*="ch"]'
    )

    count = locator.count()

    groups = []
    seen = set()

    for i in range(count):
        try:
            element = locator.nth(i)

            href = element.get_attribute("href")

            source_id = extract_channel_id(href)

            if not source_id:
                continue

            if source_id in seen:
                continue

            seen.add(source_id)

            groups.append({
                "source_id": source_id,
                "first_index": i,
            })

        except Exception:
            continue

    return groups


# ==============================================================
# KANAL -> CH ID EŞLEŞTİR
# ==============================================================

def build_channel_map(page, channels):
    """
    Tivibu sayfasında kanal listesi önce,
    program grupları sonra geliyor.

    Program gruplarının unique ch ID sırası,
    kanal listesinin sırasıyla eşleştiriliyor.

    BENİM KANALIM özellikle dışarıda bırakılır.
    """

    valid_channels = [
        channel
        for channel in channels
        if channel["name"].upper() != "BENİM KANALIM"
    ]

    groups = collect_program_groups(page)

    channel_map = {}

    limit = min(
        len(valid_channels),
        len(groups)
    )

    print(
        f"  Kanal listesi: {len(valid_channels)}"
    )

    print(
        f"  Program grubu: {len(groups)}"
    )

    if limit == 0:
        return {}

    for index in range(limit):

        channel = valid_channels[index]
        source_id = groups[index]["source_id"]

        channel["source_id"] = source_id

        channel_map[source_id] = channel["name"]

        print(
            f"    {source_id} -> {channel['name']}"
        )

    return channel_map


# ==============================================================
# PROGRAMLARI ÇEK
# ==============================================================

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

            source_id = extract_channel_id(href)

            if not source_id:
                continue

            channel_name = channel_map.get(
                source_id
            )

            if not channel_name:
                continue

            text = clean_text(
                element.inner_text()
            )

            parsed = parse_program_text(
                text
            )

            if not parsed:
                continue

            title = parsed["title"]

            if not valid_program_title(title):
                continue

            # Saat bilgisi doğrudan program linkinden alındı.
            if (
                parsed["start_hour"] is None
                or parsed["start_minute"] is None
            ):
                continue

            start = make_datetime(
                target_date,
                parsed["start_hour"],
                parsed["start_minute"],
            )

            # Tivibu yayınlarında bitiş saati ertesi güne geçebilir.
            if (
                parsed["end_hour"] is not None
                and parsed["end_minute"] is not None
            ):
                end = make_datetime(
                    target_date,
                    parsed["end_hour"],
                    parsed["end_minute"],
                )

                if end <= start:
                    end += timedelta(
                        days=1
                    )

            else:
                end = None

            key = (
                channel_name,
                start,
                title,
            )

            if key in seen:
                continue

            seen.add(key)

            programs.append({
                "channel": channel_name,
                "start": start,
                "end": end,
                "title": title,
            })

        except Exception:
            continue

    programs.sort(
        key=lambda x: (
            x["start"],
            x["channel"],
            x["title"],
        )
    )

    return programs


# ==============================================================
# TARİHİ SEÇ
# ==============================================================

def select_date(page, target_date):
    """
    Tivibu tarih butonunu DOM üzerinden bulur.

    Özellikle gerçek tarihi kullanır:
      04.09.2026
      05.09.2026
      ...

    'Bugün / Yarın' metnine güvenmez.
    """

    target_text = target_date.strftime(
        "%d.%m.%Y"
    )

    result = False

    try:
        result = page.evaluate(
            """
            (targetText) => {

                const elements = [
                    ...document.querySelectorAll("button"),
                    ...document.querySelectorAll("[role='button']"),
                    ...document.querySelectorAll("a"),
                    ...document.querySelectorAll("[class]")
                ];

                for (const el of elements) {

                    const text = (el.innerText || "").trim();

                    if (text === targetText) {

                        el.scrollIntoView({
                            block: "center"
                        });

                        el.click();

                        return true;
                    }
                }

                return false;
            }
            """,
            target_text
        )

    except Exception:
        result = False

    if not result:

        try:
            locator = page.get_by_text(
                target_text,
                exact=True
            )

            if locator.count() > 0:

                locator.first.scroll_into_view_if_needed()

                locator.first.click(
                    timeout=4000
                )

                result = True

        except Exception:
            result = False

    if result:
        page.wait_for_timeout(
            1200
        )

    return result


# ==============================================================
# TARİH DOĞRULAMA
# ==============================================================

def date_is_visible(page, target_date):
    target_text = target_date.strftime(
        "%d.%m.%Y"
    )

    try:
        return page.evaluate(
            """
            (targetText) => {
                const elements = [
                    ...document.querySelectorAll("button"),
                    ...document.querySelectorAll("[role='button']"),
                    ...document.querySelectorAll("a")
                ];

                return elements.some(
                    el => (el.innerText || "").trim() === targetText
                );
            }
            """,
            target_text
        )

    except Exception:
        return False


# ==============================================================
# BİR KATEGORİYİ ÇEK
# ==============================================================

def process_category(page, category_url, channels_master, programs_master):

    print()
    print("=" * 70)
    print(
        f"KATEGORİ: {category_url}"
    )
    print("=" * 70)

    try:
        page.goto(
            category_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

    except PlaywrightTimeoutError:
        print(
            "Sayfa yükleme zaman aşımı."
        )

    except Exception as e:
        print(
            f"Sayfa açılamadı: {e}"
        )
        return

    page.wait_for_timeout(
        2200
    )

    category_channels = collect_channels_from_page(
        page
    )

    # Benim Kanalım kesinlikle alınmaz.
    category_channels = [
        channel
        for channel in category_channels
        if channel["name"].upper() != "BENİM KANALIM"
    ]

    if not category_channels:
        print(
            "Bu kategoride kanal bulunamadı."
        )
        return

    print(
        f"Kategori kanalı: {len(category_channels)}"
    )

    # ----------------------------------------------------------
    # Kanal -> source ID eşleşmesi
    # ----------------------------------------------------------

    channel_map = build_channel_map(
        page,
        category_channels,
    )

    if not channel_map:
        print(
            "Kanal/program eşleşmesi kurulamadı."
        )
        return

    # ----------------------------------------------------------
    # 7 GÜN
    # ----------------------------------------------------------

    for day_index in range(DAYS):

        target_date = TODAY + timedelta(
            days=day_index
        )

        print()
        print(
            f"{target_date.strftime('%d.%m.%Y')}"
        )

        # İlk gün sayfanın açıldığı gündür.
        # Sonraki günlerde gerçek tarihi tıklıyoruz.
        if day_index > 0:

            selected = select_date(
                page,
                target_date
            )

            if not selected:

                print(
                    f"Tarih seçilemedi: "
                    f"{target_date.strftime('%d.%m.%Y')}"
                )

                # Sayfayı yeniden açıp tekrar dene.
                try:

                    page.reload(
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )

                    page.wait_for_timeout(
                        1800
                    )

                    selected = select_date(
                        page,
                        target_date
                    )

                except Exception:
                    selected = False

            if not selected:
                print(
                    "Bu gün atlandı."
                )
                continue

        programs = collect_programs(
            page,
            channel_map,
            target_date
        )

        print(
            f"  Program: {len(programs)}"
        )

        # ------------------------------------------------------
        # Kanal bilgilerini master listeye ekle
        # ------------------------------------------------------

        for channel in category_channels:

            name_key = channel["name"].upper()

            if name_key not in channels_master:

                channels_master[name_key] = {
                    "name": channel["name"],
                    "id": channel["id"],
                    "source_id": channel.get("source_id"),
                    "url": channel["url"],
                }

        # ------------------------------------------------------
        # Programları master listeye ekle
        # ------------------------------------------------------

        for program in programs:

            key = (
                program["channel"].upper(),
                program["start"],
                program["title"].upper(),
            )

            if key not in programs_master:
                programs_master[key] = program


# ==============================================================
# PROGRAM BİTİŞLERİNİ DÜZELT
# ==============================================================

def finalize_program_end_times(programs):

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
            end = program.get("end")

            # Site bitiş vermediyse sonraki programın başlangıcı
            # kullanılacak.
            if end is None:

                if index + 1 < len(items):

                    next_start = items[
                        index + 1
                    ]["start"]

                    end = next_start

                    if end <= start:
                        end = start + timedelta(
                            minutes=30
                        )

                else:
                    end = start + timedelta(
                        minutes=30
                    )

            # Sonraki program aynı/önceki saate düşmüşse
            # en az 30 dakika ver.
            if end <= start:
                end = start + timedelta(
                    minutes=30
                )

            # Mantıksız şekilde 12 saatten uzun programları
            # sınırla.
            if end - start > timedelta(
                hours=12
            ):
                end = start + timedelta(
                    hours=3
                )

            result.append({
                "channel": channel_name,
                "start": start,
                "end": end,
                "title": program["title"],
            })

    result.sort(
        key=lambda x: (
            x["start"],
            x["channel"],
        )
    )

    return result


# ==============================================================
# XML
# ==============================================================

def create_xml(channels, programs):

    used_names = {
        program["channel"].upper()
        for program in programs
    }

    final_channels = []

    for channel in channels:

        name = channel["name"]

        if name.upper() not in used_names:
            continue

        if name.upper() == "BENİM KANALIM":
            continue

        final_channels.append(
            channel
        )

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

    for channel in final_channels:

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

    channel_ids = {
        channel["name"].upper(): channel["id"]
        for channel in final_channels
    }

    # ----------------------------------------------------------
    # PROGRAMLAR
    # ----------------------------------------------------------

    for program in programs:

        channel_id = channel_ids.get(
            program["channel"].upper()
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

    tree = ElementTree(tv)

    try:
        import xml.etree.ElementTree as ET

        ET.indent(
            tree,
            space="  "
        )

    except Exception:
        pass

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

    global TODAY

    # GitHub Actions UTC yerine Türkiye tarihini kullan.
    now_tr = datetime.now(
        TURKEY_TZ
    )

    TODAY = now_tr.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    LAST_DAY = TODAY + timedelta(
        days=DAYS - 1
    )

    print()
    print("=" * 70)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("=" * 70)

    print(
        "Türkiye tarihi: "
        f"{TODAY.strftime('%d.%m.%Y %H:%M')}"
    )

    print(
        "Dönem: "
        f"{TODAY.strftime('%d.%m.%Y')} -> "
        f"{LAST_DAY.strftime('%d.%m.%Y')}"
    )

    print(
        f"Kategori sayısı: {len(CATEGORY_URLS)}"
    )

    print("=" * 70)

    channels_master = {}
    programs_master = {}

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
                "height": 1080,
            }
        )

        page = context.new_page()

        page.set_default_timeout(
            10000
        )

        # ------------------------------------------------------
        # KATEGORİLER
        # ------------------------------------------------------

        for category_url in CATEGORY_URLS:

            process_category(
                page,
                category_url,
                channels_master,
                programs_master
            )

        browser.close()

    # ==========================================================
    # MASTER KANALLAR
    # ==========================================================

    channels = list(
        channels_master.values()
    )

    # Programı olmayan kanal XML'e girmesin.
    used_names = {
        program["channel"].upper()
        for program in programs_master.values()
    }

    channels = [
        channel
        for channel in channels
        if channel["name"].upper() in used_names
    ]

    channels.sort(
        key=lambda x: x["name"].upper()
    )

    # ==========================================================
    # PROGRAMLAR
    # ==========================================================

    programs = list(
        programs_master.values()
    )

    programs.sort(
        key=lambda x: (
            x["start"],
            x["channel"],
            x["title"]
        )
    )

    programs = finalize_program_end_times(
        programs
    )

    # ==========================================================
    # XML
    # ==========================================================

    channel_count = create_xml(
        channels,
        programs
    )

    # ==========================================================
    # SONUÇ
    # ==========================================================

    channel_program_counts = {}

    for program in programs:

        name = program["channel"]

        channel_program_counts[name] = (
            channel_program_counts.get(
                name,
                0
            ) + 1
        )

    print()
    print("=" * 70)
    print("SONUÇ")
    print("=" * 70)

    print(
        f"Toplam kanal: {channel_count}"
    )

    print(
        f"Toplam program: {len(programs)}"
    )

    print()
    print("Kanal program sayıları:")

    for channel in channels:

        name = channel["name"]

        print(
            f"  {name}: "
            f"{channel_program_counts.get(name, 0)}"
        )

    print()
    print(
        f"{OUTPUT_FILE} oluşturuldu."
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
