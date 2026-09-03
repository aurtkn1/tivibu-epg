#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import html
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from http.cookiejar import CookieJar


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"

OUTPUT_FILE = "epg.xml"

DAYS = 7
TIMEZONE = "+0300"

REQUEST_DELAY = 0.5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


# ============================================================
# HTTP
# ============================================================

COOKIE_JAR = CookieJar()

OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(COOKIE_JAR)
)


def http_get(url, referer=None):

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "tr-TR,tr;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    if referer:
        headers["Referer"] = referer

    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )

    try:

        with OPENER.open(
            request,
            timeout=60,
        ) as response:

            raw = response.read()

            charset = (
                response.headers.get_content_charset()
                or "utf-8"
            )

            return raw.decode(
                charset,
                errors="replace",
            )

    except Exception as exc:

        raise RuntimeError(
            f"Sayfa alınamadı: {exc}"
        )


# ============================================================
# METİN TEMİZLE
# ============================================================

def clean_text(value):

    if value is None:
        return ""

    value = html.unescape(
        str(value)
    )

    value = re.sub(
        r"<script\b[^>]*>.*?</script>",
        " ",
        value,
        flags=re.I | re.S,
    )

    value = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        value,
        flags=re.I | re.S,
    )

    value = re.sub(
        r"<[^>]+>",
        " ",
        value,
    )

    value = value.replace(
        "\xa0",
        " ",
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


# ============================================================
# İSİM NORMALİZASYONU
# ============================================================

def normalize_name(value):

    value = clean_text(
        value
    ).upper()

    value = (
        value
        .replace("İ", "I")
        .replace("Ğ", "G")
        .replace("Ü", "U")
        .replace("Ş", "S")
        .replace("Ö", "O")
        .replace("Ç", "C")
    )

    value = re.sub(
        r"[^A-Z0-9]+",
        "",
        value,
    )

    return value


# ============================================================
# GERÇEK KANAL KONTROLÜ
# ============================================================

def looks_like_program(name):

    name = clean_text(
        name
    )

    if not name:
        return True

    # Saat varsa programdır.
    if re.search(
        r"\b\d{1,2}:\d{2}\b",
        name,
    ):
        return True

    # Tivibu program kartlarında kullanılan ok.
    if "→" in name:
        return True

    # Bazı programlarda saat aralığı farklı karakterle gelebilir.
    if re.search(
        r"\d{1,2}\s*-\s*\d{1,2}",
        name,
    ):
        return True

    return False


# ============================================================
# HREF'DEN KANAL SLUG
# ============================================================

def channel_slug_from_href(href):

    if not href:
        return ""

    href = html.unescape(
        href
    ).strip()

    parsed = urllib.parse.urlparse(
        href
    )

    path = parsed.path.rstrip(
        "/"
    )

    match = re.search(
        r"/kanallar/([^/?#]+)",
        path,
        re.I,
    )

    if not match:
        return ""

    return match.group(1)


# ============================================================
# GERÇEK KANALLARI HTML'DEN BUL
# ============================================================

def extract_real_channels(page):

    print()
    print(
        "[1] Gerçek kanal listesi çıkarılıyor..."
    )

    channels = {}

    # Sadece /kanallar/ bağlantıları.
    pattern = re.compile(
        r"<a\b"
        r"(?P<attrs>[^>]*)>"
        r"(?P<body>.*?)"
        r"</a>",
        re.I | re.S,
    )

    for match in pattern.finditer(
        page
    ):

        attrs = match.group(
            "attrs"
        )

        body = match.group(
            "body"
        )

        href_match = re.search(
            r'href\s*=\s*["\']([^"\']+)["\']',
            attrs,
            re.I,
        )

        if not href_match:
            continue

        href = href_match.group(
            1
        )

        slug = channel_slug_from_href(
            href
        )

        if not slug:
            continue

        name = clean_text(
            body
        )

        if not name:
            continue

        if looks_like_program(
            name
        ):
            continue

        # Aynı anchor içinde çok fazla metin varsa
        # kanal adını ayıklamaya çalış.
        #
        # İlk satır / kısa metin tercih edilir.
        pieces = [
            clean_text(x)
            for x in re.split(
                r"[\r\n]+",
                body,
            )
            if clean_text(x)
        ]

        if pieces:

            candidates = [
                x
                for x in pieces
                if not looks_like_program(x)
            ]

            if candidates:

                # En kısa makul kanal adı.
                candidates.sort(
                    key=len
                )

                candidate = candidates[0]

                if 2 <= len(candidate) <= 80:
                    name = candidate

        normalized = normalize_name(
            name
        )

        if not normalized:
            continue

        if normalized not in channels:

            full_href = (
                href
                if href.startswith(
                    "http"
                )
                else urllib.parse.urljoin(
                    BASE_URL,
                    href,
                )
            )

            channels[normalized] = {
                "name": name,
                "slug": slug,
                "href": full_href,
            }

    print(
        f"    Gerçek kanal sayısı: "
        f"{len(channels)}"
    )

    for item in sorted(
        channels.values(),
        key=lambda x:
            x["name"].lower(),
    ):

        print(
            f"      + {item['name']}"
        )

    return channels


# ============================================================
# PROGRAM SAATİ
# ============================================================

TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2})"
    r"\s*(?:→|->|–|-|—)\s*"
    r"(?P<end>\d{1,2}:\d{2})",
)


def extract_time_range(text):

    match = TIME_RANGE_RE.search(
        text
    )

    if not match:
        return None

    return (
        match.group("start"),
        match.group("end"),
    )


# ============================================================
# TARİH
# ============================================================

DATE_RE = re.compile(
    r"(?P<day>\d{1,2})"
    r"[./-]"
    r"(?P<month>\d{1,2})"
    r"[./-]"
    r"(?P<year>20\d{2})"
)


def find_date_in_text(text):

    match = DATE_RE.search(
        text
    )

    if not match:
        return None

    try:

        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        ).date()

    except ValueError:

        return None


# ============================================================
# TARİH BUTONLARI
# ============================================================

def extract_available_dates(page):

    dates = set()

    # HTML içindeki bütün tarihleri yakala.
    for match in DATE_RE.finditer(
        page
    ):

        try:

            day = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            ).date()

            dates.add(
                day
            )

        except ValueError:
            pass

    today = datetime.now().date()

    future = sorted(
        d
        for d in dates
        if d >= today
    )

    result = future[:DAYS]

    # Sayfada tarih bilgisi sınırlıysa
    # eksik günleri oluştur.
    existing = set(
        result
    )

    for offset in range(DAYS):

        day = today + timedelta(
            days=offset
        )

        if day not in existing:

            result.append(
                day
            )

    result = sorted(
        set(result)
    )[:DAYS]

    return result


# ============================================================
# PROGRAM ADINI AYIKLA
# ============================================================

def extract_program_title(
    text,
    start,
    end,
):

    text = clean_text(
        text
    )

    # Saat aralığını kaldır.
    pattern = re.escape(
        start
    ) + r"\s*(?:→|->|–|-|—)\s*" + re.escape(
        end
    )

    title = re.sub(
        pattern,
        " ",
        text,
        flags=re.I,
    )

    title = clean_text(
        title
    )

    return title


# ============================================================
# HTML'DEN PROGRAMLARI BUL
# ============================================================

def extract_programmes_from_html(
    page,
    real_channels,
):

    print()
    print(
        "[2] Programlar kanal ilişkisiyle çıkarılıyor..."
    )

    programmes = []

    # --------------------------------------------------------
    # ÖNEMLİ:
    #
    # Burada bütün HTML'i tek tek <channel> olarak
    # okumuyoruz.
    #
    # Önce gerçek /kanallar/ linkini buluyoruz.
    # Sonrasında o kanal anchor'ının bulunduğu yakın
    # HTML bloğundan saatli programları topluyoruz.
    # --------------------------------------------------------

    channel_pattern = re.compile(
        r'<a\b'
        r'(?P<attrs>[^>]*)'
        r'href\s*=\s*["\']'
        r'(?P<href>[^"\']*/kanallar/[^"\']+)'
        r'["\'][^>]*>'
        r'(?P<body>.*?)'
        r'</a>',
        re.I | re.S,
    )

    matches = list(
        channel_pattern.finditer(
            page
        )
    )

    for index, match in enumerate(
        matches
    ):

        href = match.group(
            "href"
        )

        slug = channel_slug_from_href(
            href
        )

        if not slug:
            continue

        channel_name = clean_text(
            match.group("body")
        )

        # Gerçek kanal adına göre eşleştir.
        normalized_channel = normalize_name(
            channel_name
        )

        channel = real_channels.get(
            normalized_channel
        )

        # Anchor içindeki metin program + kanal birlikte
        # olabiliyorsa /kanallar/ slug'ı üzerinden tekrar
        # gerçek kanalı bul.
        if channel is None:

            for key, value in real_channels.items():

                if value["slug"].lower() == slug.lower():

                    channel = value
                    break

        if channel is None:
            continue

        # ----------------------------------------------------
        # Kanal anchor'ından sonraki HTML bölümü.
        # Bir sonraki gerçek kanal anchor'ına kadar.
        # ----------------------------------------------------

        start_pos = match.end()

        if index + 1 < len(matches):

            end_pos = matches[
                index + 1
            ].start()

        else:

            # Son kanalda aşırı büyümemesi için sınır.
            end_pos = min(
                len(page),
                start_pos + 100000,
            )

        block = page[
            start_pos:end_pos
        ]

        # HTML tag'lerini koruyarak saatli anchor'ları bul.
        time_anchor_pattern = re.compile(
            r"<a\b"
            r"(?P<attrs>[^>]*)>"
            r"(?P<body>.*?)"
            r"</a>",
            re.I | re.S,
        )

        for pmatch in time_anchor_pattern.finditer(
            block
        ):

            body = pmatch.group(
                "body"
            )

            text = clean_text(
                body
            )

            time_range = extract_time_range(
                text
            )

            if not time_range:
                continue

            start_time, end_time = (
                time_range
            )

            title = extract_program_title(
                text,
                start_time,
                end_time,
            )

            if not title:
                continue

            # Açıkça kanal adıysa program değildir.
            if normalize_name(
                title
            ) == normalize_name(
                channel["name"]
            ):
                continue

            # Navigasyon ifadelerini at.
            blocked = {
                "DÜN",
                "BUGÜN",
                "YARIN",
                "DAHA FAZLA",
                "TÜMÜ",
                "FAVORİ",
            }

            if title.upper() in blocked:
                continue

            programmes.append(
                {
                    "channel_key":
                        normalize_name(
                            channel["name"]
                        ),

                    "channel_name":
                        channel["name"],

                    "start_time":
                        start_time,

                    "end_time":
                        end_time,

                    "title":
                        title,
                }
            )

    # --------------------------------------------------------
    # DUPLICATE TEMİZLE
    # --------------------------------------------------------

    unique = {}

    for item in programmes:

        key = (
            item["channel_key"],
            item["start_time"],
            item["end_time"],
            item["title"],
        )

        unique[key] = item

    programmes = list(
        unique.values()
    )

    print(
        f"    Bulunan program sayısı: "
        f"{len(programmes)}"
    )

    return programmes


# ============================================================
# PROGRAMLARI XML'E UYGUN HALE GETİR
# ============================================================

def make_datetime(
    date,
    time_string,
):

    hour, minute = map(
        int,
        time_string.split(":")
    )

    return datetime(
        date.year,
        date.month,
        date.day,
        hour,
        minute,
    )


def programme_to_xml_time(
    dt
):

    return (
        dt.strftime(
            "%Y%m%d%H%M%S"
        )
        + " "
        + TIMEZONE
    )


# ============================================================
# XML
# ============================================================

def create_xml(
    channels,
    programmes,
):

    print()
    print(
        "[3] XML oluşturuluyor..."
    )

    root = ET.Element(
        "tv",
        {
            "generator-info-name":
                "Tivibu 7 Günlük EPG",

            "generator-info-url":
                LIVE_TV_URL,
        }
    )

    # --------------------------------------------------------
    # KANALLAR
    # --------------------------------------------------------

    channel_id_map = {}

    for index, (
        normalized,
        channel,
    ) in enumerate(
        sorted(
            channels.items(),
            key=lambda x:
                x[1]["name"].lower(),
        ),
        start=1,
    ):

        # Stabil ID.
        channel_id = (
            "tivibu_"
            + re.sub(
                r"[^a-z0-9]+",
                "_",
                normalized.lower(),
            ).strip("_")
        )

        if not channel_id:
            channel_id = (
                f"tivibu_channel_{index}"
            )

        channel_id_map[
            normalized
        ] = channel_id

        channel_element = ET.SubElement(
            root,
            "channel",
            {
                "id":
                    channel_id
            }
        )

        display = ET.SubElement(
            channel_element,
            "display-name",
            {
                "lang":
                    "tr"
            }
        )

        display.text = channel[
            "name"
        ]

    # --------------------------------------------------------
    # PROGRAMLAR
    # --------------------------------------------------------

    programme_count = 0

    # Programları tarihe göre gruplayamıyoruz çünkü HTML
    # sayfasındaki mevcut tarih bilgisini ayrıca bulmamız
    # gerekiyor. İlk etapta bugünkü gün üzerinden yazıyoruz.
    #
    # Aşağıdaki mantık saat aralığını gece geçişlerinde
    # de doğru işler.
    # --------------------------------------------------------

    today = datetime.now().date()

    for item in programmes:

        normalized = item[
            "channel_key"
        ]

        if normalized not in channel_id_map:
            continue

        start = make_datetime(
            today,
            item["start_time"],
        )

        end = make_datetime(
            today,
            item["end_time"],
        )

        # Gece yarısını geçen program.
        if end <= start:

            end += timedelta(
                days=1
            )

        programme = ET.SubElement(
            root,
            "programme",
            {
                "start":
                    programme_to_xml_time(
                        start
                    ),

                "stop":
                    programme_to_xml_time(
                        end
                    ),

                "channel":
                    channel_id_map[
                        normalized
                    ],
            }
        )

        title = ET.SubElement(
            programme,
            "title",
            {
                "lang":
                    "tr"
            }
        )

        title.text = item[
            "title"
        ]

        programme_count += 1

    # --------------------------------------------------------
    # YAZ
    # --------------------------------------------------------

    try:

        ET.indent(
            root,
            space="  "
        )

    except AttributeError:
        pass

    tree = ET.ElementTree(
        root
    )

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )

    return programme_count


# ============================================================
# XML KONTROL
# ============================================================

def validate_xml():

    print()
    print(
        "[4] XML kontrol ediliyor..."
    )

    tree = ET.parse(
        OUTPUT_FILE
    )

    root = tree.getroot()

    channels = root.findall(
        "channel"
    )

    programmes = root.findall(
        "programme"
    )

    ids = {
        x.get("id")
        for x in channels
    }

    bad = []

    for channel in channels:

        display = channel.find(
            "display-name"
        )

        if display is None:
            bad.append(
                "display-name yok"
            )
            continue

        name = clean_text(
            display.text
        )

        if looks_like_program(
            name
        ):

            bad.append(
                name
            )

    for programme in programmes:

        channel_id = programme.get(
            "channel"
        )

        if channel_id not in ids:

            bad.append(
                f"Geçersiz kanal: {channel_id}"
            )

    print(
        f"    Channel: {len(channels)}"
    )

    print(
        f"    Programme: {len(programmes)}"
    )

    if bad:

        print()
        print(
            "HATALI KAYIT:"
        )

        for item in bad[:30]:

            print(
                f"    {item}"
            )

        raise RuntimeError(
            "XML doğrulaması başarısız."
        )

    print(
        "    XML temiz."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    started = time.time()

    print()
    print(
        "=" * 70
    )

    print(
        "TİVİBU EPG OLUŞTURUCU"
    )

    print(
        "=" * 70
    )

    # --------------------------------------------------------
    # SAYFA
    # --------------------------------------------------------

    page = http_get(
        LIVE_TV_URL
    )

    print()
    print(
        f"Tivibu HTML: "
        f"{len(page):,} karakter"
    )

    # --------------------------------------------------------
    # GERÇEK KANALLAR
    # --------------------------------------------------------

    channels = extract_real_channels(
        page
    )

    if not channels:

        raise RuntimeError(
            "Gerçek kanal bulunamadı."
        )

    # --------------------------------------------------------
    # PROGRAM
    # --------------------------------------------------------

    programmes = (
        extract_programmes_from_html(
            page,
            channels,
        )
    )

    if not programmes:

        raise RuntimeError(
            "Program bulunamadı."
        )

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    count = create_xml(
        channels,
        programmes,
    )

    # --------------------------------------------------------
    # KONTROL
    # --------------------------------------------------------

    validate_xml()

    # --------------------------------------------------------
    # SONUÇ
    # --------------------------------------------------------

    elapsed = (
        time.time()
        - started
    )

    print()
    print(
        "=" * 70
    )

    print(
        "TAMAMLANDI"
    )

    print(
        "=" * 70
    )

    print(
        f"Kanal       : {len(channels)}"
    )

    print(
        f"Program     : {count}"
    )

    print(
        f"Dosya       : {OUTPUT_FILE}"
    )

    print(
        f"Süre        : {elapsed:.1f} saniye"
    )

    print()

    print(
        "EPG:"
    )

    print(
        "https://aurtkn1.github.io/tivibu-epg/epg.xml"
    )

    print(
        "=" * 70
    )


if __name__ == "__main__":
    main()
