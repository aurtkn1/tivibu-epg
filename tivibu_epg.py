import re
import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree.ElementTree import Element, SubElement, ElementTree
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ==============================================================
# AYARLAR
# ==============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"

DAYS = 7
WORKERS = 8

TURKEY_TZ = ZoneInfo("Europe/Istanbul")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ==============================================================
# KANAL LİSTESİ
# ==============================================================

channels = {}


# ==============================================================
# TEMİZLEME
# ==============================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")
    value = value.replace("\r", "\n")

    lines = []

    for line in value.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()

        if line:
            lines.append(line)

    return "\n".join(lines)


def one_line(value):
    return re.sub(
        r"\s+",
        " ",
        html.unescape(str(value or ""))
    ).strip()


def normalize_name(name):
    return one_line(name)


def normalize_key(name):
    name = one_line(name).lower()

    replacements = {
        "ı": "i",
        "ş": "s",
        "ğ": "g",
        "ü": "u",
        "ö": "o",
        "ç": "c",
    }

    for old, new in replacements.items():
        name = name.replace(old, new)

    name = re.sub(
        r"[^a-z0-9]+",
        "",
        name
    )

    return name


def xml_id(name):
    value = one_line(name).lower()

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

    value = re.sub(
        r"[^a-z0-9]+",
        "_",
        value
    )

    return value.strip("_")


def valid_channel(name):
    if not name:
        return False

    bad = {
        "BENİM KANALIM",
        "FAVORİ KANALLARIM",
        "TİVİBU NEDİR?",
        "TİVİBU CANLI TV",
        "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
        "TRT1 CANLI İZLE",
        "TRT 1 CANLI İZLE",
        "TİVİBU SPOR CANLI İZLE",
        "CANLI TV",
        "KANALLAR",
        "PROGRAMLAR",
        "ANA SAYFA",
    }

    if name.upper() in bad:
        return False

    if len(name) > 100:
        return False

    return True


def valid_title(title):
    title = one_line(title)

    if not title:
        return False

    if len(title) > 300:
        return False

    bad = {
        "CANLI TV",
        "KANALLAR",
        "PROGRAMLAR",
        "ANA SAYFA",
        "FAVORİ KANALLARIM",
        "TİVİBU NEDİR?",
        "TİVİBU CANLI TV",
        "TRT1 CANLI İZLE",
        "TRT 1 CANLI İZLE",
    }

    if title.upper() in bad:
        return False

    return True


# ==============================================================
# KANAL LİSTESİ
# ==============================================================

def collect_channels(page):

    result = {}

    locator = page.locator(
        'a[href*="/kanallar/"]'
    )

    count = locator.count()

    for i in range(count):

        try:

            element = locator.nth(i)

            href = element.get_attribute(
                "href"
            )

            name = one_line(
                element.inner_text()
            )

            if not href or not name:
                continue

            if not valid_channel(name):
                continue

            if href.startswith("/"):
                href = BASE_URL + href

            key = normalize_key(
                name
            )

            if key not in result:

                result[key] = {
                    "name": name,
                    "url": href,
                    "id": xml_id(name),
                }

        except Exception:
            continue

    return result


# ==============================================================
# KANAL SAYFASINDAN PROGRAM AL
# ==============================================================

def extract_schedule_block(page):

    """
    Sayfadaki 'Günün Programları' bölümünün metnini çıkarır.
    SEO metninin programa karışmasını engellemek için
    Bilgilendirme öncesinde kesilir.
    """

    try:

        body = page.locator(
            "body"
        ).inner_text()

    except Exception:

        return ""

    body = clean_text(
        body
    )

    if not body:
        return ""

    lines = [
        one_line(x)
        for x in body.split("\n")
        if one_line(x)
    ]

    start_index = None
    end_index = len(lines)

    for i, line in enumerate(lines):

        if normalize_key(
            line
        ) == normalize_key(
            "Günün Programları"
        ):

            start_index = i
            break

    if start_index is None:
        return ""

    for i in range(
        start_index + 1,
        len(lines)
    ):

        if normalize_key(
            lines[i]
        ) == normalize_key(
            "Bilgilendirme"
        ):

            end_index = i
            break

    return "\n".join(
        lines[
            start_index + 1:
            end_index
        ]
    )


# ==============================================================
# PROGRAM SATIRLARINI PARSE ET
# ==============================================================

TIME_RE = re.compile(
    r"^(\d{1,2}):(\d{2})\s*-\s*"
    r"(\d{1,2}):(\d{2})$"
)


def parse_schedule_text(
    schedule_text,
    target_date,
    channel_name
):

    lines = [
        one_line(x)
        for x in schedule_text.split("\n")
        if one_line(x)
    ]

    programs = []

    pending_title = None

    for line in lines:

        # ------------------------------------------------------
        # "Canlı Yayın" Tivibu'nun araya koyduğu işaret.
        # Program adı değildir.
        # ------------------------------------------------------

        if normalize_key(
            line
        ) == normalize_key(
            "Canlı Yayın"
        ):
            continue

        match = TIME_RE.match(
            line
        )

        if match:

            if not pending_title:
                continue

            sh = int(
                match.group(1)
            )

            sm = int(
                match.group(2)
            )

            eh = int(
                match.group(3)
            )

            em = int(
                match.group(4)
            )

            start = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                sh,
                sm,
                tzinfo=TURKEY_TZ
            )

            end = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                eh,
                em,
                tzinfo=TURKEY_TZ
            )

            if end <= start:
                end += timedelta(
                    days=1
                )

            title = one_line(
                pending_title
            )

            if valid_title(title):

                programs.append({
                    "channel": channel_name,
                    "title": title,
                    "start": start,
                    "end": end,
                })

            pending_title = None
            continue

        # ------------------------------------------------------
        # "Günün Programları" bölümündeki bütün normal metinler
        # bir sonraki program başlığı olarak değerlendirilir.
        # ------------------------------------------------------

        if len(line) <= 300:
            pending_title = line

    # ----------------------------------------------------------
    # Aynı kayıtları temizle
    # ----------------------------------------------------------

    unique = []
    seen = set()

    for program in programs:

        key = (
            normalize_key(
                program["channel"]
            ),
            program["start"],
            normalize_key(
                program["title"]
            )
        )

        if key in seen:
            continue

        seen.add(key)

        unique.append(
            program
        )

    return unique


# ==============================================================
# TARİH SEÇİMİ
# ==============================================================

def click_date(page, target_date):

    variants = [
        target_date.strftime(
            "%d.%m.%Y"
        ),
        target_date.strftime(
            "%d/%m/%Y"
        ),
        target_date.strftime(
            "%Y-%m-%d"
        ),
        target_date.strftime(
            "%d.%m"
        ),
    ]

    # ----------------------------------------------------------
    # JS ile gerçek tarih butonunu bul
    # ----------------------------------------------------------

    try:

        clicked = page.evaluate(
            """
            variants => {

                const elements = [
                    ...document.querySelectorAll("button"),
                    ...document.querySelectorAll("[role='button']"),
                    ...document.querySelectorAll("a")
                ];

                for (const el of elements) {

                    const text =
                        (el.innerText || "")
                        .trim();

                    if (variants.includes(text)) {

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
            variants
        )

        if clicked:

            page.wait_for_timeout(
                1000
            )

            return True

    except Exception:
        pass

    # ----------------------------------------------------------
    # get_by_text fallback
    # ----------------------------------------------------------

    for value in variants:

        try:

            locator = page.get_by_text(
                value,
                exact=True
            )

            if locator.count() > 0:

                locator.first.click(
                    timeout=2500
                )

                page.wait_for_timeout(
                    1000
                )

                return True

        except Exception:
            continue

    return False


# ==============================================================
# KANAL SAYFASINDA 7 GÜN
# ==============================================================

def scrape_channel(
    channel,
    today
):

    local_programs = []

    with sync_playwright() as p:

        browser = None

        try:

            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
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
                7000
            )

            try:

                page.goto(
                    channel["url"],
                    wait_until="domcontentloaded",
                    timeout=30000
                )

            except PlaywrightTimeoutError:
                pass

            page.wait_for_timeout(
                800
            )

            for day_index in range(DAYS):

                target_date = (
                    today
                    + timedelta(
                        days=day_index
                    )
                )

                # --------------------------------------------------
                # İlk gün sayfa ilk açıldığında gelen gündür.
                # --------------------------------------------------

                if day_index == 0:

                    pass

                else:

                    selected = click_date(
                        page,
                        target_date
                    )

                    if not selected:

                        # Kanal sayfasında tarih seçici yoksa
                        # canlı-tv sayfasından tarih değiştirme
                        # denenmez; aynı günü tekrar üretmeyelim.
                        break

                    page.wait_for_timeout(
                        900
                    )

                schedule = extract_schedule_block(
                    page
                )

                if not schedule:
                    continue

                programs = parse_schedule_text(
                    schedule,
                    target_date,
                    channel["name"]
                )

                local_programs.extend(
                    programs
                )

        except Exception as e:

            print(
                f"  HATA: {channel['name']} -> {e}"
            )

        finally:

            if browser:

                try:
                    browser.close()
                except Exception:
                    pass

    return local_programs


# ==============================================================
# TARİH VE SAATLERİ DÜZELT
# ==============================================================

def normalize_programs(programs):

    unique = []
    seen = set()

    for program in programs:

        key = (
            normalize_key(
                program["channel"]
            ),
            program["start"],
            normalize_key(
                program["title"]
            )
        )

        if key in seen:
            continue

        seen.add(key)

        unique.append(
            program
        )

    # ----------------------------------------------------------
    # Kanal bazında sırala
    # ----------------------------------------------------------

    grouped = {}

    for program in unique:

        grouped.setdefault(
            normalize_key(
                program["channel"]
            ),
            []
        ).append(
            program
        )

    final = []

    for channel_key, items in grouped.items():

        items.sort(
            key=lambda x: x["start"]
        )

        for i, program in enumerate(items):

            start = program["start"]
            end = program.get("end")

            if end is None:

                if i + 1 < len(items):

                    end = items[
                        i + 1
                    ]["start"]

                else:

                    end = (
                        start
                        + timedelta(
                            minutes=30
                        )
                    )

            if end <= start:

                end = (
                    start
                    + timedelta(
                        minutes=30
                    )
                )

            if (
                end - start
                > timedelta(hours=12)
            ):

                end = (
                    start
                    + timedelta(
                        hours=3
                    )
                )

            final.append({
                "channel": program["channel"],
                "title": program["title"],
                "start": start,
                "end": end,
            })

    final.sort(
        key=lambda x: (
            x["start"],
            normalize_key(
                x["channel"]
            )
        )
    )

    return final


# ==============================================================
# XML
# ==============================================================

def write_xml(
    channel_data,
    programs
):

    used_channel_keys = {
        normalize_key(
            p["channel"]
        )
        for p in programs
    }

    final_channels = []

    for key, channel in channel_data.items():

        if key not in used_channel_keys:
            continue

        if channel["name"].upper() == "BENİM KANALIM":
            continue

        final_channels.append(
            channel
        )

    final_channels.sort(
        key=lambda x: x["name"].upper()
    )

    tv = Element(
        "tv",
        {
            "generator-info-name":
                "Tivibu 7 Günlük EPG",
            "generator-info-url":
                "https://www.tivibu.com.tr/",
        }
    )

    # ----------------------------------------------------------
    # CHANNEL
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
        normalize_key(
            channel["name"]
        ):
            channel["id"]
        for channel in final_channels
    }

    # ----------------------------------------------------------
    # PROGRAM
    # ----------------------------------------------------------

    for program in programs:

        channel_id = channel_ids.get(
            normalize_key(
                program["channel"]
            )
        )

        if not channel_id:
            continue

        programme = SubElement(
            tv,
            "programme",
            {
                "channel": channel_id,
                "start": (
                    program["start"]
                    .strftime(
                        "%Y%m%d%H%M%S"
                    )
                    + " +0300"
                ),
                "stop": (
                    program["end"]
                    .strftime(
                        "%Y%m%d%H%M%S"
                    )
                    + " +0300"
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

    tree = ElementTree(
        tv
    )

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

    today = datetime.now(
        TURKEY_TZ
    ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    print("=" * 70)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("=" * 70)

    print(
        f"Dönem: "
        f"{today.strftime('%d.%m.%Y')} -> "
        f"{(
            today
            + timedelta(days=DAYS - 1)
        ).strftime('%d.%m.%Y')}"
    )

    print("=" * 70)

    # ----------------------------------------------------------
    # Kanal listesini bir kez al
    # ----------------------------------------------------------

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
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
            10000
        )

        print(
            "Tivibu kanal listesi alınıyor..."
        )

        try:

            page.goto(
                LIVE_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

        except PlaywrightTimeoutError:

            print(
                "Ana sayfa yükleme zaman aşımı."
            )

        page.wait_for_timeout(
            2000
        )

        channel_data = collect_channels(
            page
        )

        browser.close()

    print(
        f"Bulunan gerçek kanal: "
        f"{len(channel_data)}"
    )

    # ----------------------------------------------------------
    # KANALLARI TEK TEK KANAL SAYFALARINDAN AL
    # ----------------------------------------------------------

    all_programs = []

    channels_list = list(
        channel_data.values()
    )

    completed = 0

    print()
    print(
        f"{len(channels_list)} kanal taranacak."
    )

    # Thread başına Playwright instance.
    # 8 paralel iş sayesinde eski 10-15 dakikalık yapı
    # ciddi şekilde kısalır.
    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = {
            executor.submit(
                scrape_channel,
                channel,
                today
            ):
                channel
            for channel in channels_list
        }

        for future in as_completed(
            futures
        ):

            channel = futures[
                future
            ]

            try:

                programs = future.result()

                all_programs.extend(
                    programs
                )

                completed += 1

                print(
                    f"[{completed}/{len(channels_list)}] "
                    f"{channel['name']}: "
                    f"{len(programs)} program"
                )

            except Exception as e:

                completed += 1

                print(
                    f"[{completed}/{len(channels_list)}] "
                    f"{channel['name']}: "
                    f"HATA - {e}"
                )

    # ----------------------------------------------------------
    # TEMİZLE
    # ----------------------------------------------------------

    programs = normalize_programs(
        all_programs
    )

    # ----------------------------------------------------------
    # SONUÇ
    # ----------------------------------------------------------

    print()
    print("=" * 70)
    print("PROGRAM ÖZETİ")
    print("=" * 70)

    counts = {}

    for program in programs:

        key = program["channel"]

        counts[key] = (
            counts.get(
                key,
                0
            )
            + 1
        )

    for channel_name in sorted(
        counts
    ):

        print(
            f"  {channel_name}: "
            f"{counts[channel_name]}"
        )

    print()
    print(
        f"Program bulunan kanal: "
        f"{len(counts)}"
    )

    print(
        f"Toplam program: "
        f"{len(programs)}"
    )

    # ----------------------------------------------------------
    # XML
    # ----------------------------------------------------------

    channel_count = write_xml(
        channel_data,
        programs
    )

    print()
    print("=" * 70)
    print("SONUÇ")
    print("=" * 70)

    print(
        f"XML kanal: {channel_count}"
    )

    print(
        f"XML program: {len(programs)}"
    )

    print(
        f"Dosya: {OUTPUT_FILE}"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
