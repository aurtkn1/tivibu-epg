import re
import json
import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from xml.etree.ElementTree import Element, SubElement, ElementTree
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ==============================================================
# AYARLAR
# ==============================================================

BASE_URL = "https://www.tivibu.com.tr/canli-tv"
OUTPUT_FILE = "epg.xml"
DAYS = 7

TURKEY_TZ = ZoneInfo("Europe/Istanbul")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ==============================================================
# YARDIMCILAR
# ==============================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_key(value):
    return re.sub(
        r"[^a-z0-9]+",
        "",
        clean_text(value).lower()
    )


def channel_xml_id(name):
    value = clean_text(name).lower()

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

    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")

    return value


def valid_channel_name(name):
    name = clean_text(name)

    if not name:
        return False

    bad = {
        "BENİM KANALIM",
        "FAVORİ KANALLARIM",
        "TRT1 CANLI İZLE",
        "TRT 1 CANLI İZLE",
        "TİVİBU SPOR CANLI İZLE",
        "CANLI TV",
        "KANALLAR",
        "PROGRAMLAR",
        "TİVİBU NEDİR?",
        "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
        "ANA SAYFA",
    }

    if name.upper() in bad:
        return False

    if len(name) > 100:
        return False

    return True


def valid_program_title(title):
    title = clean_text(title)

    if not title:
        return False

    if len(title) > 300:
        return False

    bad = {
        "CANLI TV",
        "KANALLAR",
        "PROGRAMLAR",
        "ANA SAYFA",
        "TİVİBU",
        "FAVORİ KANALLARIM",
        "TRT1 CANLI İZLE",
        "TRT 1 CANLI İZLE",
    }

    if title.upper() in bad:
        return False

    return True


def make_datetime(date_obj, hour, minute):
    return datetime(
        date_obj.year,
        date_obj.month,
        date_obj.day,
        hour,
        minute,
        tzinfo=TURKEY_TZ,
    )


def xml_datetime(dt):
    return dt.strftime("%Y%m%d%H%M%S") + " +0300"


def parse_clock(value):
    if value is None:
        return None

    text = clean_text(value)

    match = re.search(
        r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b",
        text
    )

    if not match:
        return None

    return int(match.group(1)), int(match.group(2))


def parse_datetime_value(value, fallback_date=None):
    if value is None:
        return None

    # ----------------------------------------------------------
    # Sayısal Unix timestamp
    # ----------------------------------------------------------

    if isinstance(value, (int, float)):

        try:
            number = float(value)

            if number > 100000000000:
                number /= 1000

            if number > 1000000000:
                return datetime.fromtimestamp(
                    number,
                    tz=TURKEY_TZ
                )

        except Exception:
            pass

        return None

    text = clean_text(value)

    if not text:
        return None

    # ----------------------------------------------------------
    # ISO tarih
    # ----------------------------------------------------------

    iso_text = text.replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(
            iso_text
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=TURKEY_TZ
            )

        return dt.astimezone(
            TURKEY_TZ
        )

    except Exception:
        pass

    # ----------------------------------------------------------
    # Türkçe tarih formatları
    # ----------------------------------------------------------

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ]

    for fmt in formats:

        try:
            dt = datetime.strptime(
                text,
                fmt
            )

            return dt.replace(
                tzinfo=TURKEY_TZ
            )

        except Exception:
            pass

    # ----------------------------------------------------------
    # Sadece saat varsa
    # ----------------------------------------------------------

    clock = parse_clock(text)

    if clock and fallback_date:

        hour, minute = clock

        return make_datetime(
            fallback_date,
            hour,
            minute
        )

    return None


# ==============================================================
# JSON AĞ DEPOLAMA
# ==============================================================

class NetworkStore:

    def __init__(self):
        self.responses = []
        self.seen_urls = set()

    def attach(self, page):
        page.on(
            "response",
            self.on_response
        )

    def on_response(self, response):

        try:
            url = response.url

            if url in self.seen_urls:
                return

            content_type = (
                response.headers.get(
                    "content-type",
                    ""
                ).lower()
            )

            interesting_url = any(
                word in url.lower()
                for word in (
                    "api",
                    "graphql",
                    "epg",
                    "program",
                    "programme",
                    "channel",
                    "schedule",
                    "broadcast",
                    "tv",
                    "content",
                    "catchup",
                    "live",
                )
            )

            is_json = (
                "json" in content_type
                or "javascript" in content_type
            )

            if not interesting_url and not is_json:
                return

            body = response.text()

            if not body:
                return

            if len(body) > 10_000_000:
                return

            body = body.strip()

            if not (
                body.startswith("{")
                or body.startswith("[")
            ):
                return

            data = json.loads(
                body
            )

            self.seen_urls.add(
                url
            )

            self.responses.append({
                "url": url,
                "data": data,
            })

        except Exception:
            return


# ==============================================================
# KANALLAR
# ==============================================================

def collect_all_channels(page):
    channels = {}
    locator = page.locator(
        'a[href*="/kanallar/"]'
    )

    count = locator.count()

    for i in range(count):

        try:
            element = locator.nth(i)

            name = clean_text(
                element.inner_text()
            )

            href = element.get_attribute(
                "href"
            )

            if not name or not href:
                continue

            if not valid_channel_name(name):
                continue

            if href.startswith("/"):
                href = (
                    "https://www.tivibu.com.tr"
                    + href
                )

            key = normalize_key(
                name
            )

            if key in channels:
                continue

            channels[key] = {
                "name": name,
                "url": href,
                "id": channel_xml_id(name),
                "source_id": None,
            }

        except Exception:
            continue

    return channels


# ==============================================================
# JSON ANAHTAR SINIFLANDIRMA
# ==============================================================

def key_is_channel_name(key):
    k = normalize_key(key)

    return any(
        x in k
        for x in (
            "channelname",
            "channeltitle",
            "channeldisplayname",
            "displayname",
            "channame",
        )
    )


def key_is_channel_id(key):
    k = normalize_key(key)

    return any(
        x in k
        for x in (
            "channelid",
            "channelkey",
            "channelcode",
            "channeluuid",
            "stationid",
        )
    )


def key_is_title(key):
    k = normalize_key(key)

    return (
        k in {
            "title",
            "programtitle",
            "programname",
            "program",
            "programme",
            "contenttitle",
            "contentname",
            "eventtitle",
            "eventname",
            "name",
        }
        or "programtitle" in k
        or "programname" in k
        or "eventtitle" in k
    )


def key_is_start(key):
    k = normalize_key(key)

    return any(
        x in k
        for x in (
            "starttime",
            "startdate",
            "startdatetime",
            "begintime",
            "begindate",
            "fromtime",
            "fromdate",
            "airtime",
            "onairtime",
        )
    )


def key_is_end(key):
    k = normalize_key(key)

    return any(
        x in k
        for x in (
            "endtime",
            "enddate",
            "enddatetime",
            "stoptime",
            "stopdate",
            "totime",
            "todate",
        )
    )


# ==============================================================
# JSON'DAN ID / İSİM HARİTASI ÇIKAR
# ==============================================================

def build_json_channel_maps(data):

    id_to_name = {}
    aliases = {}

    def walk(value, inherited_channel=None):

        if isinstance(value, list):

            for item in value:
                walk(
                    item,
                    inherited_channel
                )

            return

        if not isinstance(value, dict):
            return

        local_name = inherited_channel
        local_id = None

        for key, item in value.items():

            if not isinstance(item, (dict, list)):

                if key_is_channel_name(key):

                    text = clean_text(
                        item
                    )

                    if valid_channel_name(text):
                        local_name = text

                elif key_is_channel_id(key):

                    text = clean_text(
                        item
                    )

                    if text:
                        local_id = text

        if local_id and local_name:
            id_to_name[
                local_id
            ] = local_name

            aliases[
                normalize_key(local_id)
            ] = local_name

        for key, item in value.items():

            if isinstance(item, (dict, list)):

                walk(
                    item,
                    local_name
                )

    walk(data)

    return id_to_name, aliases


# ==============================================================
# JSON PROGRAM KAYITLARINI BUL
# ==============================================================

def extract_program_records(
    data,
    fallback_date,
    json_channel_maps,
):

    records = []

    id_to_name = json_channel_maps

    def walk(value, context=None):

        if isinstance(value, list):

            for item in value:
                walk(
                    item,
                    context
                )

            return

        if not isinstance(value, dict):
            return

        local_channel_name = context
        local_channel_id = None

        title = None
        start_value = None
        end_value = None

        # ------------------------------------------------------
        # Önce mevcut object
        # ------------------------------------------------------

        for key, item in value.items():

            if isinstance(item, (dict, list)):
                continue

            text = clean_text(item)

            if key_is_channel_name(key):

                if valid_channel_name(text):
                    local_channel_name = text

            elif key_is_channel_id(key):

                if text:
                    local_channel_id = text

            elif key_is_title(key):

                if text and len(text) <= 300:
                    title = text

            elif key_is_start(key):

                if text:
                    start_value = item

            elif key_is_end(key):

                if text:
                    end_value = item

        # ------------------------------------------------------
        # ID üzerinden kanal bul
        # ------------------------------------------------------

        if (
            not local_channel_name
            and local_channel_id
        ):

            local_channel_name = (
                id_to_name.get(
                    local_channel_id
                )
            )

            if not local_channel_name:
                local_channel_name = (
                    id_to_name.get(
                        normalize_key(
                            local_channel_id
                        )
                    )
                )

        # ------------------------------------------------------
        # Bu obje program olabilir
        # ------------------------------------------------------

        if title and start_value is not None:

            start = parse_datetime_value(
                start_value,
                fallback_date
            )

            end = None

            if end_value is not None:
                end = parse_datetime_value(
                    end_value,
                    fallback_date
                )

            if start:

                records.append({
                    "channel": local_channel_name,
                    "channel_id": local_channel_id,
                    "title": title,
                    "start": start,
                    "end": end,
                })

        # ------------------------------------------------------
        # Alt objeler
        # ------------------------------------------------------

        child_context = (
            local_channel_name
            or context
        )

        for key, item in value.items():

            if isinstance(item, (dict, list)):

                walk(
                    item,
                    child_context
                )

    walk(
        data
    )

    return records


# ==============================================================
# AĞ VERİLERİNİ İŞLE
# ==============================================================

def extract_network_programs(
    network_store,
    target_date,
):

    all_records = []

    global_channel_map = {}

    # ----------------------------------------------------------
    # Önce tüm channel ID -> name haritalarını topla
    # ----------------------------------------------------------

    for response in network_store.responses:

        try:

            id_map, alias_map = (
                build_json_channel_maps(
                    response["data"]
                )
            )

            global_channel_map.update(
                id_map
            )

            global_channel_map.update(
                alias_map
            )

        except Exception:
            continue

    # ----------------------------------------------------------
    # Program kayıtlarını çıkar
    # ----------------------------------------------------------

    for response in network_store.responses:

        try:

            records = extract_program_records(
                response["data"],
                target_date,
                global_channel_map
            )

            all_records.extend(
                records
            )

        except Exception:
            continue

    return all_records


# ==============================================================
# DOM FALLBACK
# ==============================================================

def extract_dom_programs(
    page,
    target_date,
):

    programs = []

    # Ana sayfadaki gerçek program linklerini kullan.
    locator = page.locator(
        'a[href*="/rv?"]'
    )

    count = locator.count()

    for i in range(count):

        try:

            element = locator.nth(i)

            text = clean_text(
                element.inner_text()
            )

            if not text:
                continue

            # Örnek:
            # Program Film - 23:30 → 01:15 Canlı
            match = re.match(
                r"^(.*?)\s+"
                r"(?:Film|Dizi|Yaşam|Spor|Haber|Belgesel|"
                r"Çocuk|Müzik|Eğlence|Aktüalite|Diğer)"
                r"\s*-\s*"
                r"(\d{1,2})[:.](\d{2})"
                r"\s*→\s*"
                r"(\d{1,2})[:.](\d{2})"
                r"(?:\s+Canlı)?$",
                text,
                re.IGNORECASE
            )

            if not match:
                continue

            title = clean_text(
                match.group(1)
            )

            if not valid_program_title(title):
                continue

            sh = int(
                match.group(2)
            )
            sm = int(
                match.group(3)
            )
            eh = int(
                match.group(4)
            )
            em = int(
                match.group(5)
            )

            start = make_datetime(
                target_date,
                sh,
                sm
            )

            end = make_datetime(
                target_date,
                eh,
                em
            )

            if end <= start:
                end += timedelta(
                    days=1
                )

            # ID
            href = element.get_attribute(
                "href"
            )

            source_id = None

            if href:
                m = re.search(
                    r"\bch[a-zA-Z0-9]+\b",
                    href
                )

                if m:
                    source_id = m.group(0)

            programs.append({
                "channel": None,
                "channel_id": source_id,
                "title": title,
                "start": start,
                "end": end,
            })

        except Exception:
            continue

    return programs


# ==============================================================
# SAHTE PROGRAMLARI TEMİZLE
# ==============================================================

def clean_programs(
    records,
    channels,
):

    channel_by_name = {
        normalize_key(
            c["name"]
        ): c
        for c in channels.values()
    }

    clean = []
    seen = set()

    for record in records:

        title = clean_text(
            record.get("title")
        )

        if not valid_program_title(title):
            continue

        channel_name = clean_text(
            record.get("channel")
        )

        # ------------------------------------------------------
        # Kanal adı varsa
        # ------------------------------------------------------

        channel = None

        if channel_name:

            channel = channel_by_name.get(
                normalize_key(
                    channel_name
                )
            )

        # ------------------------------------------------------
        # Kanal ID üzerinden dene
        # ------------------------------------------------------

        if channel is None:

            channel_id = clean_text(
                record.get(
                    "channel_id"
                )
            )

            if channel_id:

                # JSON id'sini kaynak haritasında değilse
                # bazı API'lerde ID doğrudan kanal linkindeki
                # değere karşılık gelebilir.
                for c in channels.values():

                    if c.get(
                        "source_id"
                    ) == channel_id:

                        channel = c
                        break

        if channel is None:
            continue

        start = record.get(
            "start"
        )

        end = record.get(
            "end"
        )

        if not start:
            continue

        key = (
            channel["name"].upper(),
            start,
            title.upper(),
        )

        if key in seen:
            continue

        seen.add(key)

        clean.append({
            "channel": channel["name"],
            "start": start,
            "end": end,
            "title": title,
        })

    return clean


# ==============================================================
# KANAL PROGRAM BİTİŞLERİ
# ==============================================================

def complete_end_times(programs):

    grouped = defaultdict(list)

    for program in programs:

        grouped[
            program["channel"]
        ].append(
            program
        )

    result = []

    for channel_name, items in grouped.items():

        items.sort(
            key=lambda x: x["start"]
        )

        for i, program in enumerate(items):

            start = program["start"]
            end = program.get(
                "end"
            )

            if end is None:

                if i + 1 < len(items):

                    end = items[
                        i + 1
                    ]["start"]

                else:

                    end = start + timedelta(
                        minutes=30
                    )

            if end <= start:
                end = start + timedelta(
                    minutes=30
                )

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

    return result


# ==============================================================
# TARİH SEÇ
# ==============================================================

def select_date(page, target_date):

    target = target_date.strftime(
        "%d.%m.%Y"
    )

    clicked = False

    # ----------------------------------------------------------
    # Exact text
    # ----------------------------------------------------------

    try:

        loc = page.get_by_text(
            target,
            exact=True
        )

        count = loc.count()

        for i in range(count):

            try:

                item = loc.nth(i)

                box = item.bounding_box()

                if box is not None:

                    item.scroll_into_view_if_needed(
                        timeout=2000
                    )

                    item.click(
                        timeout=3000
                    )

                    clicked = True
                    break

            except Exception:
                continue

    except Exception:
        pass

    # ----------------------------------------------------------
    # JS fallback
    # ----------------------------------------------------------

    if not clicked:

        try:

            clicked = page.evaluate(
                """
                target => {

                    const items = [
                        ...document.querySelectorAll(
                            "button"
                        ),
                        ...document.querySelectorAll(
                            "[role='button']"
                        ),
                        ...document.querySelectorAll(
                            "a"
                        )
                    ];

                    for (
                        const el of items
                    ) {

                        const text =
                            (el.innerText || "")
                            .trim();

                        if (text === target) {

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
                target
            )

        except Exception:
            clicked = False

    if clicked:

        page.wait_for_timeout(
            1800
        )

    return clicked


# ==============================================================
# XML
# ==============================================================

def write_xml(
    channels,
    programs,
):

    used = {
        normalize_key(
            p["channel"]
        )
        for p in programs
    }

    final_channels = []

    for key, channel in channels.items():

        if key not in used:
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
            c["name"]
        ): c["id"]
        for c in final_channels
    }

    programs.sort(
        key=lambda x: (
            x["start"],
            x["channel"],
        )
    )

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

    today = datetime.now(
        TURKEY_TZ
    ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    last_day = today + timedelta(
        days=DAYS - 1
    )

    print("=" * 70)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("=" * 70)

    print(
        f"Dönem: "
        f"{today.strftime('%d.%m.%Y')} -> "
        f"{last_day.strftime('%d.%m.%Y')}"
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
                "height": 1080,
            }
        )

        page = context.new_page()

        page.set_default_timeout(
            10000
        )

        network = NetworkStore()

        network.attach(
            page
        )

        # ------------------------------------------------------
        # SAYFAYI AÇ
        # ------------------------------------------------------

        print(
            "Tivibu açılıyor..."
        )

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

        page.wait_for_timeout(
            3000
        )

        # ------------------------------------------------------
        # TÜM KANALLAR
        # ------------------------------------------------------

        channels = collect_all_channels(
            page
        )

        print()
        print(
            f"Tivibu kanal listesi: "
            f"{len(channels)}"
        )

        # ------------------------------------------------------
        # JSON NETWORK VERİLERİ
        # ------------------------------------------------------

        print(
            f"Yakalanan JSON yanıtı: "
            f"{len(network.responses)}"
        )

        # ------------------------------------------------------
        # SOURCE ID'LERİ
        # ------------------------------------------------------

        json_channel_names = {}

        for response in network.responses:

            try:

                id_map, alias_map = (
                    build_json_channel_maps(
                        response["data"]
                    )
                )

                json_channel_names.update(
                    id_map
                )

                json_channel_names.update(
                    alias_map
                )

            except Exception:
                continue

        print(
            f"JSON kanal eşleşmesi: "
            f"{len(json_channel_names)}"
        )

        # ------------------------------------------------------
        # SOURCE ID'LERİNİ KANALLARA ATA
        # ------------------------------------------------------

        for source_id, name in json_channel_names.items():

            normalized = normalize_key(
                name
            )

            if normalized in channels:

                channels[
                    normalized
                ]["source_id"] = source_id

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

                selected = select_date(
                    page,
                    target_date
                )

                if not selected:

                    print(
                        "Tarih seçilemedi."
                    )

                    # Sayfayı yenileyip tekrar dene.
                    try:

                        page.reload(
                            wait_until="domcontentloaded",
                            timeout=60000
                        )

                        page.wait_for_timeout(
                            2500
                        )

                        select_date(
                            page,
                            target_date
                        )

                    except Exception:
                        pass

            # --------------------------------------------------
            # Ağdan veri
            # --------------------------------------------------

            network_records = (
                extract_network_programs(
                    network,
                    target_date
                )
            )

            print(
                f"Ağ program kaydı: "
                f"{len(network_records)}"
            )

            clean_network = clean_programs(
                network_records,
                channels
            )

            print(
                f"Ağdan geçerli program: "
                f"{len(clean_network)}"
            )

            # --------------------------------------------------
            # DOM fallback
            # --------------------------------------------------

            if len(clean_network) == 0:

                dom_records = (
                    extract_dom_programs(
                        page,
                        target_date
                    )
                )

                print(
                    f"DOM program kaydı: "
                    f"{len(dom_records)}"
                )

                # DOM kayıtlarında ID varsa
                # JSON ID -> kanal adından çöz.
                for record in dom_records:

                    source_id = record.get(
                        "channel_id"
                    )

                    if not source_id:
                        continue

                    source_name = (
                        json_channel_names.get(
                            source_id
                        )
                    )

                    if source_name:

                        record["channel"] = (
                            source_name
                        )

                clean_dom = clean_programs(
                    dom_records,
                    channels
                )

                print(
                    f"DOM'dan geçerli program: "
                    f"{len(clean_dom)}"
                )

                all_programs.extend(
                    clean_dom
                )

            else:

                all_programs.extend(
                    clean_network
                )

            # --------------------------------------------------
            # Programları kanal bazında göster
            # --------------------------------------------------

            day_counts = defaultdict(
                int
            )

            for program in all_programs:

                if (
                    program["start"].date()
                    == target_date.date()
                ):

                    day_counts[
                        program["channel"]
                    ] += 1

            for channel_name, count in sorted(
                day_counts.items()
            ):

                print(
                    f"  {channel_name}: "
                    f"{count}"
                )

        browser.close()

    # ==========================================================
    # DUPLICATE
    # ==========================================================

    unique = []
    seen = set()

    for program in all_programs:

        key = (
            normalize_key(
                program["channel"]
            ),
            program["start"],
            program["title"].upper()
        )

        if key in seen:
            continue

        seen.add(key)

        unique.append(
            program
        )

    # ==========================================================
    # SAAT SINIRLARI
    # ==========================================================

    unique = complete_end_times(
        unique
    )

    # ==========================================================
    # PROGRAMI OLAN KANALLAR
    # ==========================================================

    used = {
        normalize_key(
            p["channel"]
        )
        for p in unique
    }

    final_channels = {}

    for key, channel in channels.items():

        if key not in used:
            continue

        if channel["name"].upper() == "BENİM KANALIM":
            continue

        final_channels[key] = channel

    # ==========================================================
    # XML
    # ==========================================================

    channel_count = write_xml(
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
        f"Toplam kanal: "
        f"{channel_count}"
    )

    print(
        f"Toplam program: "
        f"{len(unique)}"
    )

    print()
    print(
        "Program bulunan kanallar:"
    )

    counts = defaultdict(int)

    for program in unique:

        counts[
            program["channel"]
        ] += 1

    for name in sorted(
        counts
    ):

        print(
            f"  {name}: "
            f"{counts[name]}"
        )

    print()
    print(
        f"{OUTPUT_FILE} oluşturuldu."
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
