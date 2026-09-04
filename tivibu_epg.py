import re
import html
import urllib.request
import urllib.error
from html.parser import HTMLParser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from xml.etree.ElementTree import Element, SubElement, ElementTree


# ==============================================================
# AYARLAR
# ==============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_URL = f"{BASE_URL}/canli-tv"

OUTPUT_FILE = "epg.xml"

WORKERS = 12

TURKEY_TZ = ZoneInfo("Europe/Istanbul")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

TODAY = datetime.now(
    TURKEY_TZ
).date()

NOW = datetime.now(
    TURKEY_TZ
)


# ==============================================================
# TEMEL
# ==============================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_key(value):
    value = clean_text(value).lower()

    replacements = {
        "ı": "i",
        "ş": "s",
        "ğ": "g",
        "ü": "u",
        "ö": "o",
        "ç": "c",
        "’": "",
        "'": "",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    return re.sub(
        r"[^a-z0-9]+",
        "",
        value
    )


def make_xml_id(name):
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
        "’": "_",
        "'": "_",
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
    name = clean_text(name)

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
        "GİRİŞ YAP",
        "ÜYE OL",
    }

    if name.upper() in bad:
        return False

    if len(name) > 100:
        return False

    return True


def valid_title(title):
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
        "FAVORİ KANALLARIM",
        "TİVİBU NEDİR?",
        "TRT1 CANLI İZLE",
        "TRT 1 CANLI İZLE",
        "GİRİŞ YAP",
        "ÜYE OL",
        "BENİM KANALIM",
    }

    if title.upper() in bad:
        return False

    return True


# ==============================================================
# HTTP
# ==============================================================

def fetch_url(url, timeout=25):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=timeout
        ) as response:

            data = response.read()

            charset = response.headers.get_content_charset()

            if not charset:
                charset = "utf-8"

            return data.decode(
                charset,
                errors="replace"
            )

    except Exception as e:

        raise RuntimeError(
            f"{url} -> {e}"
        )


# ==============================================================
# HTML LINK PARSER
# ==============================================================

class LinkParser(HTMLParser):

    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )

        self.links = []

        self.current_href = None
        self.current_text = []

    def handle_starttag(
        self,
        tag,
        attrs
    ):

        if tag.lower() != "a":
            return

        attrs_dict = dict(
            attrs
        )

        self.current_href = (
            attrs_dict.get(
                "href"
            )
        )

        self.current_text = []

    def handle_data(self, data):

        if self.current_href is not None:

            self.current_text.append(
                data
            )

    def handle_endtag(self, tag):

        if tag.lower() != "a":
            return

        if self.current_href is not None:

            text = clean_text(
                " ".join(
                    self.current_text
                )
            )

            self.links.append({
                "href": self.current_href,
                "text": text,
            })

        self.current_href = None
        self.current_text = []


def parse_links(source):

    parser = LinkParser()

    parser.feed(
        source
    )

    return parser.links


# ==============================================================
# KANALLARI ANA SAYFADAN BUL
# ==============================================================

def collect_channels(source):

    links = parse_links(
        source
    )

    channels = {}

    for link in links:

        href = link["href"]
        text = link["text"]

        if not href or not text:
            continue

        if not href.startswith(
            "/kanallar/"
        ):
            continue

        # Program linklerini alma.
        if "→" in text:
            continue

        if " - " in text and re.search(
            r"\d{1,2}:\d{2}",
            text
        ):
            continue

        if href.startswith("/"):
            href = BASE_URL + href

        href = (
            href
            .split("?", 1)[0]
            .split("#", 1)[0]
            .rstrip("/")
        )

        name = text

        if not valid_channel(name):
            continue

        # URL son parçasından /kanal/ olmayan şeyleri ele.
        path = href.rstrip("/").split(
            "/"
        )[-1]

        if not path:
            continue

        key = normalize_key(
            name
        )

        if not key:
            continue

        if key in channels:
            continue

        channels[key] = {
            "name": name,
            "url": href,
            "id": make_xml_id(name),
        }

    return channels


# ==============================================================
# PROGRAM SATIRI
#
# Örnek:
#
# Kanal D Ana Haber Aktüalite - 19:00 → 20:00 Canlı
# ==============================================================

PROGRAM_RE = re.compile(
    r"^(?P<title>.*?)"
    r"\s+"
    r"(?P<category>"
    r"Film|"
    r"Dizi|"
    r"Yaşam|"
    r"Spor Programı|"
    r"Spor|"
    r"Haber|"
    r"Belgesel|"
    r"Çocuk|"
    r"Müzik|"
    r"Eğlence|"
    r"Aktüalite|"
    r"Diğer"
    r")"
    r"\s*-\s*"
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})"
    r"\s*→\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})"
    r"(?:\s+Canlı)?$",
    re.IGNORECASE
)


def parse_program_line(text):

    text = clean_text(
        text
    )

    match = PROGRAM_RE.match(
        text
    )

    if not match:
        return None

    title = clean_text(
        match.group(
            "title"
        )
    )

    if not valid_title(title):
        return None

    return {
        "title": title,
        "start_hour": int(
            match.group("sh")
        ),
        "start_minute": int(
            match.group("sm")
        ),
        "end_hour": int(
            match.group("eh")
        ),
        "end_minute": int(
            match.group("em")
        ),
    }


# ==============================================================
# PROGRAM TARİHLERİNİ OLUŞTUR
#
# Kanal sayfasında örneğin:
#
# 23:55 -> 00:35
# 00:35 -> 01:10
# 01:10 -> 02:15
#
# ilk kayıt önceki geceye ait olabilir.
#
# Bu yüzden saat akışını takip ediyoruz.
# ==============================================================

def convert_schedule_to_programs(
    channel_name,
    entries
):

    if not entries:
        return []

    # ----------------------------------------------------------
    # İlk programın gününü tahmin et.
    #
    # İlk saat şu anki saatten ilerideyse genellikle
    # kanal sayfası önceki gecenin devam eden programıyla
    # başlıyor.
    # ----------------------------------------------------------

    first = entries[0]

    first_time = (
        first["start_hour"],
        first["start_minute"]
    )

    now_time = (
        NOW.hour,
        NOW.minute
    )

    current_date = TODAY

    if first_time > now_time:

        current_date = (
            TODAY
            - timedelta(
                days=1
            )
        )

    programs = []

    previous_minutes = None

    for entry in entries:

        start_minutes = (
            entry["start_hour"] * 60
            + entry["start_minute"]
        )

        # ------------------------------------------------------
        # Saat geriye döndüyse gece yarısı geçildi.
        # ------------------------------------------------------

        if (
            previous_minutes is not None
            and start_minutes < previous_minutes
        ):

            current_date += timedelta(
                days=1
            )

        start = datetime(
            current_date.year,
            current_date.month,
            current_date.day,
            entry["start_hour"],
            entry["start_minute"],
            tzinfo=TURKEY_TZ
        )

        end_date = current_date

        end_minutes = (
            entry["end_hour"] * 60
            + entry["end_minute"]
        )

        if end_minutes <= start_minutes:

            end_date = (
                current_date
                + timedelta(
                    days=1
                )
            )

        end = datetime(
            end_date.year,
            end_date.month,
            end_date.day,
            entry["end_hour"],
            entry["end_minute"],
            tzinfo=TURKEY_TZ
        )

        programs.append({
            "channel": channel_name,
            "title": entry["title"],
            "start": start,
            "end": end,
        })

        previous_minutes = (
            start_minutes
        )

    return programs


# ==============================================================
# KANAL SAYFASINI PARSE ET
# ==============================================================

def scrape_channel(channel):

    url = channel["url"]

    try:

        source = fetch_url(
            url,
            timeout=30
        )

    except Exception as e:

        return {
            "channel": channel,
            "programs": [],
            "error": str(e),
        }

    links = parse_links(
        source
    )

    entries = []

    for link in links:

        text = clean_text(
            link["text"]
        )

        if not text:
            continue

        if "→" not in text:
            continue

        # Program satırı olmalı.
        if not re.search(
            r"\d{1,2}:\d{2}\s*→\s*\d{1,2}:\d{2}",
            text
        ):
            continue

        parsed = parse_program_line(
            text
        )

        if not parsed:
            continue

        entries.append(
            parsed
        )

    # ----------------------------------------------------------
    # Aynı programları tekrar eden HTML linklerini temizle.
    # ----------------------------------------------------------

    unique_entries = []

    seen = set()

    for entry in entries:

        key = (
            entry["title"],
            entry["start_hour"],
            entry["start_minute"],
            entry["end_hour"],
            entry["end_minute"],
        )

        if key in seen:
            continue

        seen.add(key)

        unique_entries.append(
            entry
        )

    programs = convert_schedule_to_programs(
        channel["name"],
        unique_entries
    )

    # ----------------------------------------------------------
    # SADECE BUGÜN
    # ----------------------------------------------------------

    programs = [
        program
        for program in programs
        if program["start"].date()
        == TODAY
    ]

    return {
        "channel": channel,
        "programs": programs,
        "error": None,
    }


# ==============================================================
# PROGRAMLARI TEMİZLE
# ==============================================================

def clean_programs(programs):

    result = []

    seen = set()

    for program in programs:

        title = clean_text(
            program["title"]
        )

        if not valid_title(title):
            continue

        if (
            program["start"].date()
            != TODAY
        ):
            continue

        key = (
            normalize_key(
                program["channel"]
            ),
            program["start"],
            normalize_key(
                title
            ),
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        result.append({
            "channel": program["channel"],
            "title": title,
            "start": program["start"],
            "end": program["end"],
        })

    result.sort(
        key=lambda x: (
            x["channel"],
            x["start"]
        )
    )

    return result


# ==============================================================
# BİTİŞ SAATLERİNİ GÜVENLİ HALE GETİR
# ==============================================================

def fix_end_times(programs):

    grouped = defaultdict(list)

    for program in programs:

        grouped[
            normalize_key(
                program["channel"]
            )
        ].append(
            program
        )

    result = []

    for _, items in grouped.items():

        items.sort(
            key=lambda x: x["start"]
        )

        for i, program in enumerate(
            items
        ):

            start = program["start"]
            end = program["end"]

            if end <= start:

                end = (
                    start
                    + timedelta(
                        minutes=30
                    )
                )

            # Bir sonraki program başlangıcı mevcut
            # bitişten daha erkense, mevcut bitişi düzelt.
            if i + 1 < len(items):

                next_start = items[
                    i + 1
                ]["start"]

                if (
                    next_start > start
                    and
                    next_start < end
                ):
                    end = next_start

            if end - start > timedelta(
                hours=12
            ):
                end = (
                    start
                    + timedelta(
                        hours=3
                    )
                )

            result.append({
                "channel": program["channel"],
                "title": program["title"],
                "start": start,
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

def write_xml(
    channels,
    programs
):

    used = {
        normalize_key(
            program["channel"]
        )
        for program in programs
    }

    final_channels = []

    for key, channel in channels.items():

        if key not in used:
            continue

        if (
            channel["name"].upper()
            == "BENİM KANALIM"
        ):
            continue

        if (
            channel["name"].upper()
            == "TİVİBU SPOR CANLI İZLE"
        ):
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
                "Tivibu Günlük EPG",
            "generator-info-url":
                "https://www.tivibu.com.tr/",
        }
    )

    # ----------------------------------------------------------
    # CHANNEL
    # ----------------------------------------------------------

    for channel in final_channels:

        element = SubElement(
            tv,
            "channel",
            {
                "id": channel["id"]
            }
        )

        display = SubElement(
            element,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display.text = channel["name"]

    channel_ids = {
        normalize_key(
            channel["name"]
        ): channel["id"]
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

        element = SubElement(
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
            element,
            "title",
            {
                "lang": "tr"
            }
        )

        title.text = program["title"]

    # ----------------------------------------------------------
    # YAZ
    # ----------------------------------------------------------

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

    print("=" * 70)
    print("TIVIBU GÜNLÜK EPG")
    print("=" * 70)

    print(
        "Türkiye tarihi: "
        f"{TODAY.strftime('%d.%m.%Y')}"
    )

    print(
        "Saat: "
        f"{NOW.strftime('%H:%M:%S')}"
    )

    print("=" * 70)

    # ==========================================================
    # 158 KANALI TEK SEFER BUL
    # ==========================================================

    print(
        "Ana kanal listesi alınıyor..."
    )

    try:

        main_source = fetch_url(
            LIVE_URL,
            timeout=40
        )

    except Exception as e:

        print(
            f"Ana sayfa alınamadı: {e}"
        )

        return

    channels = collect_channels(
        main_source
    )

    print(
        f"Bulunan gerçek kanal: "
        f"{len(channels)}"
    )

    if not channels:

        print(
            "Hiç kanal bulunamadı."
        )

        return

    # ==========================================================
    # 158 KANAL SAYFASINI PARALEL AL
    # ==============================================================

    all_programs = []

    successful = 0
    failed = 0

    channel_list = list(
        channels.values()
    )

    print()
    print(
        f"{len(channel_list)} kanalın "
        "GÜNLÜK programı alınıyor..."
    )

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        future_map = {
            executor.submit(
                scrape_channel,
                channel
            ):
                channel
            for channel in channel_list
        }

        completed = 0

        for future in as_completed(
            future_map
        ):

            channel = future_map[
                future
            ]

            completed += 1

            try:

                result = future.result()

                programs = result[
                    "programs"
                ]

                if result["error"]:

                    failed += 1

                    print(
                        f"[{completed}/{len(channel_list)}] "
                        f"{channel['name']}: "
                        f"HATA"
                    )

                    continue

                successful += 1

                all_programs.extend(
                    programs
                )

                print(
                    f"[{completed}/{len(channel_list)}] "
                    f"{channel['name']}: "
                    f"{len(programs)} program"
                )

            except Exception as e:

                failed += 1

                print(
                    f"[{completed}/{len(channel_list)}] "
                    f"{channel['name']}: "
                    f"HATA - {e}"
                )

    # ==========================================================
    # TEMİZLE
    # ==========================================================

    all_programs = clean_programs(
        all_programs
    )

    all_programs = fix_end_times(
        all_programs
    )

    # ==========================================================
    # KANAL İSTATİSTİKLERİ
    # ==============================================================

    counts = defaultdict(
        int
    )

    for program in all_programs:

        counts[
            program["channel"]
        ] += 1

    # ==========================================================
    # XML
    # ==============================================================

    xml_channels = write_xml(
        channels,
        all_programs
    )

    # ==========================================================
    # SONUÇ
    # ==============================================================

    print()
    print("=" * 70)
    print("SONUÇ")
    print("=" * 70)

    print(
        f"Toplam keşfedilen kanal: "
        f"{len(channels)}"
    )

    print(
        f"Başarılı kanal sayfası: "
        f"{successful}"
    )

    print(
        f"Hatalı kanal sayfası: "
        f"{failed}"
    )

    print(
        f"XML kanal: "
        f"{xml_channels}"
    )

    print(
        f"Toplam program: "
        f"{len(all_programs)}"
    )

    print()
    print(
        "KANAL PROGRAM SAYILARI:"
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
        f"{OUTPUT_FILE} oluşturuldu."
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
