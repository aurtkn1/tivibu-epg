#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import html
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
PREVUE_URL = f"{BASE_URL}/Channel/GetPrevueList"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

OUTPUT_FILE = "epg.xml"

# İstekler arasında kısa bekleme.
REQUEST_DELAY = 0.25


def http_request(url, data=None, headers=None):
    """
    Tivibu'ya HTTP isteği gönderir.
    """
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(
        url=url,
        data=data,
        headers=request_headers,
        method="POST" if data is not None else "GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")

    except Exception as exc:
        raise RuntimeError(f"HTTP isteği başarısız: {url}\n{exc}") from exc


def get_main_page():
    """
    Tivibu canlı TV sayfasını alır.
    """
    print("[1/4] Tivibu canlı TV sayfası alınıyor...")
    return http_request(
        LIVE_TV_URL,
        headers={
            "Referer": BASE_URL + "/",
        },
    )


def extract_csrf_token(page):
    """
    Sayfadaki CSRF form token'ını bulur.

    Aranan alan:
    CSRF-TOKEN-TVBUDNBX!-FORM
    """

    patterns = [
        # name="..." value="..."
        r'name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\'][^>]*value=["\']([^"\']+)["\']',

        # value="..." name="..."
        r'value=["\']([^"\']+)["\'][^>]*name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\']',

        # Tek tırnak / çift tırnak karışık durumlar
        r'name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\'][^>]*value\s*=\s*["\']([^"\']+)["\']',
    ]

    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE)
        if match:
            token = html.unescape(match.group(1)).strip()

            if token:
                return token

    # Bazı sayfalarda token farklı HTML düzeninde bulunabiliyor.
    generic_match = re.search(
        r'CSRF-TOKEN-TVBUDNBX!-FORM.{0,500}?value=["\']([^"\']+)["\']',
        page,
        re.IGNORECASE | re.DOTALL,
    )

    if generic_match:
        token = html.unescape(generic_match.group(1)).strip()

        if token:
            return token

    raise RuntimeError(
        "CSRF token bulunamadı. "
        "Tivibu sayfasındaki CSRF-TOKEN-TVBUDNBX!-FORM alanı değişmiş olabilir."
    )


def extract_channel_list(page):
    """
    Tivibu canlı TV HTML'sinden kanal adı + channelCode listesini çıkarır.

    Örnek:
        TİVİBU TANITIM -> ch00000000000000001358
    """

    print("[2/4] Kanal listesi HTML'den çıkarılıyor...")

    channels = []
    seen = set()

    # channelsTitle bloklarını buluyoruz.
    title_pattern = re.compile(
        r'<div\s+class=["\']channelsTitle["\'][^>]*>'
        r'(.*?)'
        r'</div>',
        re.IGNORECASE | re.DOTALL,
    )

    title_blocks = title_pattern.findall(page)

    for title_block in title_blocks:
        # Kanal adı.
        title_match = re.search(
            r'<a[^>]*title=["\']([^"\']+)["\'][^>]*>'
            r'(.*?)'
            r'</a>',
            title_block,
            re.IGNORECASE | re.DOTALL,
        )

        if not title_match:
            # title attribute bulunmazsa iç metni deneyelim.
            title_match = re.search(
                r'<a[^>]*>(.*?)</a>',
                title_block,
                re.IGNORECASE | re.DOTALL,
            )

            if not title_match:
                continue

            channel_name = re.sub(r"<[^>]+>", "", title_match.group(1))
        else:
            channel_name = title_match.group(1)

        channel_name = html.unescape(
            re.sub(r"\s+", " ", channel_name)
        ).strip()

        if not channel_name:
            continue

        # Aynı başlık bloğunun civarında channelCode bulmak için
        # HTML içerisinde takip eden alanı arıyoruz.
        # Önce bloğun bulunduğu noktayı sayfada tekrar buluyoruz.
        block_index = page.find(title_block)

        if block_index < 0:
            continue

        # Bir sonraki kanal başlığına kadar bak.
        next_title = page.find(
            '<div class="channelsTitle"',
            block_index + len(title_block),
        )

        if next_title < 0:
            next_title = len(page)

        nearby_html = page[block_index:next_title]

        code_match = re.search(
            r'ch\d{20,}',
            nearby_html,
            re.IGNORECASE,
        )

        if not code_match:
            continue

        channel_code = code_match.group(0)

        if channel_code in seen:
            continue

        seen.add(channel_code)

        channels.append(
            {
                "name": channel_name,
                "code": channel_code,
            }
        )

    # Yukarıdaki HTML bölme yöntemi bazı tekrarları kaçırırsa,
    # doğrudan sayfadaki benzersiz channelCode'ları da tespit ediyoruz.
    all_codes = []
    for match in re.finditer(r'ch\d{20,}', page, re.IGNORECASE):
        code = match.group(0)

        if code not in all_codes:
            all_codes.append(code)

    # Bilinen eşleşmeler dışında kalan kodları da ekle.
    known_codes = {x["code"] for x in channels}

    for code in all_codes:
        if code not in known_codes:
            channels.append(
                {
                    "name": code,
                    "code": code,
                }
            )

    return channels


def post_prevue(channel_code, csrf_token):
    """
    GetPrevueList endpoint'ine Tivibu'nun tarayıcıdaki POST isteğini gönderir.
    """

    form_data = urllib.parse.urlencode(
        {
            "channelCode": channel_code,
            "CSRF-TOKEN-TVBUDNBX!-FORM": csrf_token,
        }
    ).encode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": BASE_URL,
        "Referer": LIVE_TV_URL,
        "X-Requested-With": "XMLHttpRequest",
        "RequestVerificationToken": csrf_token,
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    response = http_request(
        PREVUE_URL,
        data=form_data,
        headers=headers,
    )

    return response


def parse_programs(response_text):
    """
    GetPrevueList JSON yanıtından programları çıkarır.
    """

    import json

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        raise RuntimeError(
            "GetPrevueList JSON döndürmedi.\n"
            f"İlk 500 karakter:\n{response_text[:500]}"
        )

    programs = data.get("mobilPrevueListViewModel") or []

    if not programs:
        programs = data.get("prevueListViewModel") or []

    return programs


def parse_datetime(value):
    """
    Tivibu tarih formatını datetime nesnesine çevirir.

    Örnek:
        2026.09.03 01:15:00
    """

    if not value:
        return None

    value = value.strip()

    formats = [
        "%Y.%m.%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass

    return None


def add_text(parent, tag, text):
    """
    XML içerisine metin ekler.
    """
    element = ET.SubElement(parent, tag)
    element.text = text or ""
    return element


def create_xmltv(channels, channel_programs):
    """
    XMLTV dosyasını oluşturur.
    """

    print("[4/4] XMLTV dosyası oluşturuluyor...")

    tv = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu EPG GitHub",
            "generator-info-url": "https://www.tivibu.com.tr/canli-tv",
        },
    )

    # Kanal bilgileri.
    for channel in channels:
        channel_element = ET.SubElement(
            tv,
            "channel",
            {
                "id": channel["code"],
            },
        )

        add_text(
            channel_element,
            "display-name",
            channel["name"],
        )

    # Programlar.
    for channel in channels:
        channel_code = channel["code"]
        channel_name = channel["name"]

        programs = channel_programs.get(channel_code, [])

        for program in programs:
            title = (
                program.get("prevueName")
                or program.get("name")
                or "Bilinmeyen Program"
            )

            description = program.get("description") or ""
            genre = program.get("genre") or ""

            begin_value = program.get("beginTime") or ""
            end_value = program.get("endTime") or ""

            begin_dt = parse_datetime(begin_value)
            end_dt = parse_datetime(end_value)

            if not begin_dt or not end_dt:
                continue

            # XMLTV formatında local timezone bilinmediği için
            # +0300 kullanıyoruz (Türkiye).
            start_attr = begin_dt.strftime("%Y%m%d%H%M%S") + " +0300"
            stop_attr = end_dt.strftime("%Y%m%d%H%M%S") + " +0300"

            programme = ET.SubElement(
                tv,
                "programme",
                {
                    "start": start_attr,
                    "stop": stop_attr,
                    "channel": channel_code,
                },
            )

            add_text(
                programme,
                "title",
                title,
            )

            if description:
                add_text(
                    programme,
                    "desc",
                    description,
                )

            if genre:
                category = ET.SubElement(programme, "category")
                category.text = genre

            release_year = program.get("releaseYear") or ""

            if release_year.isdigit():
                date_element = ET.SubElement(
                    programme,
                    "date",
                )
                date_element.text = release_year

    tree = ET.ElementTree(tv)

    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        # Python < 3.9 için sorun çıkarmasın.
        pass

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )


def main():
    start_time = time.time()

    print("=" * 70)
    print("TIVIBU EPG TEST / XMLTV OLUŞTURUCU")
    print("=" * 70)

    # 1) Ana sayfayı al.
    page = get_main_page()

    # 2) CSRF token.
    print("[2/4] CSRF token aranıyor...")
    csrf_token = extract_csrf_token(page)

    print("      CSRF token bulundu.")

    # 3) Kanal listesi.
    channels = extract_channel_list(page)

    if not channels:
        raise RuntimeError(
            "Hiç kanal bulunamadı."
        )

    print(f"      Bulunan benzersiz kanal sayısı: {len(channels)}")

    # İlk birkaç kanalı göster.
    print()
    print("İlk kanallar:")
    for channel in channels[:15]:
        print(
            f"  - {channel['name']} "
            f"-> {channel['code']}"
        )

    print()
    print("[3/4] Tivibu GetPrevueList çağrıları yapılıyor...")

    channel_programs = {}

    success_count = 0
    fail_count = 0
    total_programs = 0

    for index, channel in enumerate(channels, start=1):
        channel_name = channel["name"]
        channel_code = channel["code"]

        print(
            f"[{index:03d}/{len(channels):03d}] "
            f"{channel_name} "
            f"({channel_code})"
        )

        try:
            response = post_prevue(
                channel_code,
                csrf_token,
            )

            programs = parse_programs(response)

            channel_programs[channel_code] = programs

            success_count += 1
            total_programs += len(programs)

            print(
                f"       OK - {len(programs)} program"
            )

        except Exception as exc:
            fail_count += 1

            print(
                f"       HATA - {exc}"
            )

            # Hatalı kanalın XML'i bozmasını engelle.
            channel_programs[channel_code] = []

        time.sleep(REQUEST_DELAY)

    # XML oluştur.
    create_xmltv(
        channels,
        channel_programs,
    )

    elapsed = time.time() - start_time

    print()
    print("=" * 70)
    print("TAMAMLANDI")
    print("=" * 70)
    print(f"Kanal sayısı       : {len(channels)}")
    print(f"Başarılı kanallar  : {success_count}")
    print(f"Hatalı kanallar    : {fail_count}")
    print(f"Toplam program     : {total_programs}")
    print(f"Oluşturulan dosya  : {OUTPUT_FILE}")
    print(f"Süre               : {elapsed:.1f} saniye")
    print("=" * 70)


if __name__ == "__main__":
    main()
