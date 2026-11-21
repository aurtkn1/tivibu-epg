
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Tivibu 7 günlük EPG oluşturucu.

Programlar Tivibu'nun MultiPrevue servisi üzerinden alınır ve
channelCode alanıyla kanal kaydına doğrudan bağlanır.
Mevcut epg.xml, bütün kontroller geçmeden değiştirilmez.
"""

import html
import http.cookiejar
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo


BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
MULTI_PREVUE_URL = f"{BASE_URL}/Channel/GetMultiPrevueData"
OUTPUT_FILE = "epg.xml"
DAYS = 7
TIMEZONE = ZoneInfo("Europe/Istanbul")
REQUEST_TIMEOUT = 60
RETRY_COUNT = 3
REQUEST_DELAY = 0.35
MIN_CHANNELS = 20
MIN_PROGRAMS = 100

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

COOKIE_JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(COOKIE_JAR)
)


class PageAttributeParser(HTMLParser):
    """HTML içindeki tarih bağlantılarını ve form alanlarını toplar."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.date_options = []
        self.fields = {}
        self._current_date = None
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(k).lower(): (v or "") for k, v in attrs}
        tag = tag.lower()

        if tag == "input":
            name = attrs.get("name", "").strip()
            value = attrs.get("value", "").strip()
            if name:
                self.fields[name.lower()] = value

        if tag == "a":
            begin = attrs.get("channeldatebegin", "").strip()
            end = attrs.get("channeldateend", "").strip()
            if begin and end:
                self._current_date = {"begin": begin, "end": end}
                self._current_text = []

        for key, value in attrs.items():
            normalized = key.lower().replace("-", "").replace("_", "")
            if normalized in ("channelcolumncode", "channelsearchvalue") and value:
                self.fields[normalized] = value.strip()

    def handle_data(self, data):
        if self._current_date is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._current_date is not None:
            item = dict(self._current_date)
            item["text"] = clean_text(" ".join(self._current_text))
            self.date_options.append(item)
            self._current_date = None
            self._current_text = []


def clean_text(value):
    if value is None:
        return ""
    value = html.unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def http_request(url, data=None, headers=None):
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
    }

    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(
        url=url,
        data=data,
        headers=request_headers,
        method="POST" if data is not None else "GET",
    )

    last_error = None

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            with OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")

        except Exception as exc:
            last_error = exc
            if attempt < RETRY_COUNT:
                time.sleep(attempt * 1.5)

    raise RuntimeError(
        f"HTTP isteği {RETRY_COUNT} denemede başarısız oldu.\n"
        f"URL: {url}\nHata: {last_error}"
    ) from last_error


def get_main_page():
    print("[1/4] Tivibu canlı TV sayfası alınıyor...")

    return http_request(
        LIVE_TV_URL,
        headers={"Referer": BASE_URL + "/"},
    )


def extract_page_data(page):
    parser = PageAttributeParser()
    parser.feed(page)
    parser.close()

    raw_patterns = {
        "channelcolumncode": [
            r"channelColumnCode\s*=\s*['\"]([^'\"]+)['\"]",
            r"channelColumnCode\s*:\s*['\"]([^'\"]+)['\"]",
            r"['\"]channelColumnCode['\"]\s*:\s*['\"]([^'\"]+)['\"]",
        ],
        "channelsearchvalue": [
            r"channelSearchValue\s*=\s*['\"]([^'\"]*)['\"]",
            r"channelSearchValue\s*:\s*['\"]([^'\"]*)['\"]",
            r"['\"]channelSearchValue['\"]\s*:\s*['\"]([^'\"]*)['\"]",
        ],
    }

    source = html.unescape(page)

    for field, patterns in raw_patterns.items():
        if parser.fields.get(field):
            continue

        for pattern in patterns:
            match = re.search(pattern, source, re.IGNORECASE)
            if match:
                parser.fields[field] = match.group(1).strip()
                break

    csrf_token = ""

    csrf_names = {
        "csrf-token-tvbudnbx!-form",
        "requestverificationtoken",
        "__requestverificationtoken",
    }

    for name, value in parser.fields.items():
        if name.lower() in csrf_names and value:
            csrf_token = value
            break

    if not csrf_token:
        csrf_patterns = [
            r'name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\'][^>]*value=["\']([^"\']+)',
            r'value=["\']([^"\']+)["\'][^>]*name=["\']CSRF-TOKEN-TVBUDNBX!-FORM',
            r'CSRF-TOKEN-TVBUDNBX!-FORM["\']?\s*[:=]\s*["\']([^"\']+)',
            r'CSRF-TOKEN-TVBUDNBX!-FORM.{0,500}?value=["\']([^"\']+)',
        ]

        for pattern in csrf_patterns:
            match = re.search(
                pattern,
                source,
                re.IGNORECASE | re.DOTALL
            )

            if match:
                csrf_token = html.unescape(match.group(1)).strip()
                if csrf_token:
                    break

    if not csrf_token:
        raise RuntimeError(
            "CSRF token bulunamadı. Tivibu sayfa yapısı değişmiş olabilir."
        )

    column_code = parser.fields.get("channelcolumncode", "").strip()
    search_value = parser.fields.get("channelsearchvalue", "").strip()

    if not column_code:
        raise RuntimeError(
            "channelColumnCode bulunamadı. "
            "Servise boş kanal koduyla istek gönderilmedi."
        )

    return csrf_token, column_code, search_value, parser.date_options


def parse_tivibu_date(value):
    value = clean_text(value)

    if not value:
        return None

    formats = (
        "%Y.%m.%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
        "%Y.%m.%d",
        "%Y-%m-%d",
    )

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    return None


def build_target_dates(date_options):
    today = datetime.now(TIMEZONE).date()
    found = {}

    for item in date_options:
        dt = parse_tivibu_date(item.get("begin", ""))

        if dt and dt.date() >= today:
            found.setdefault(dt.date(), item)

    targets = [found[day] for day in sorted(found)[:DAYS]]

    if len(targets) != DAYS:
        available = ", ".join(
            sorted(day.strftime("%d.%m.%Y") for day in found)
        )

        raise RuntimeError(
            f"Tivibu sayfasında {DAYS} ileri tarih bulunamadı; "
            f"{len(targets)} tarih bulundu. Mevcut XML korunuyor. "
            f"Bulunan tarihler: {available or 'yok'}"
        )

    return targets


def post_multi_prevue(
    csrf_token,
    channel_column_code,
    channel_search_value,
    begin_date,
    end_date
):
    form = {
        "channelColumnCode": channel_column_code,
        "channelDateBegin": begin_date,
        "channelDateEnd": end_date,
        "channelSearchValue": channel_search_value,
        "pageNo": "1",
    }

    body = urllib.parse.urlencode(form).encode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": BASE_URL,
        "Referer": LIVE_TV_URL,
        "X-Requested-With": "XMLHttpRequest",
        "RequestVerificationToken": csrf_token,
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    response = http_request(
        MULTI_PREVUE_URL,
        data=body,
        headers=headers
    )

    try:
        data = json.loads(response)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "EPG servisi JSON döndürmedi. İlk yanıt bölümü: "
            + clean_text(response[:350])
        ) from exc

    if isinstance(data, dict) and isinstance(data.get("d"), str):
        try:
            nested = json.loads(data["d"])

            if isinstance(nested, dict):
                data = nested

        except json.JSONDecodeError:
            pass

    if not isinstance(data, dict):
        raise RuntimeError(
            "EPG servisi beklenen JSON nesnesini döndürmedi."
        )

    return data


def first_nonempty(item, keys):
    if not isinstance(item, dict):
        return ""

    for key in keys:
        value = clean_text(item.get(key))

        if value:
            return value

    return ""


def unpack_collection(value):
    """Servisin liste, JSON metni veya sarmalanmış nesne döndürmesini çözer."""

    if value is None:
        return []

    if isinstance(value, list):
        return value

    if isinstance(value, str):
        text = value.strip()

        if not text:
            return []

        try:
            return unpack_collection(json.loads(text))

        except json.JSONDecodeError:
            return []

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = (
                str(key).lower().replace("_", "").replace("-", "")
            )

            if normalized in {
                "items",
                "data",
                "list",
                "result",
                "rows",
                "channels",
                "programs",
                "prevues",
                "channelitems",
                "prevueitems",
                "channellistviewmodel",
                "prevuelistviewmodel",
            }:
                found = unpack_collection(nested)

                if found:
                    return found

        if any(k in value for k in (
            "channelCode",
            "ChannelCode",
            "prevueName",
            "PrevueName",
            "beginTime",
            "BeginTime"
        )):
            return [value]

    return []


def get_list(data, keys):
    lowered = {
        str(k).lower(): v
        for k, v in data.items()
    }

    for key in keys:
        value = data.get(key)

        if value is None:
            value = lowered.get(key.lower())

        result = unpack_collection(value)

        if result:
            return result

    return []


def describe_service_fields(data):
    """Boş listenin gerçek nedenini bulmak için sınırlı yanıt özeti üretir."""

    names = (
        "columnChannelViewModel",
        "channelListViewModel",
        "channelMobilListViewModel",
        "prevueListViewModel",
        "firtPrevueViewModel",
        "mobilPrevueViewModel",
    )

    details = []

    for name in names:
        value = data.get(name)

        if value is None:
            value = next(
                (
                    v for k, v in data.items()
                    if str(k).lower() == name.lower()
                ),
                None
            )

        if isinstance(value, (list, dict)):
            preview = json.dumps(
                value[:1] if isinstance(value, list) else value,
                ensure_ascii=False
            )

            details.append(
                f"{name}: tür={type(value).__name__}, "
                f"adet={len(value)}, örnek={preview[:900]}"
            )

        elif isinstance(value, str):
            details.append(
                f"{name}: tür=str, uzunluk={len(value)}, "
                f"örnek={value[:500]}"
            )

        else:
            details.append(
                f"{name}: tür={type(value).__name__}, değer={value!r}"
            )

    return "\n".join(details)


def normalize_channels(data):
    channels = {}

    items = get_list(data, [
        "channelListViewModel",
        "ChannelListViewModel",
        "channels",
        "Channels",
    ])

    for item in items:
        code = first_nonempty(
            item,
            ["channelCode", "ChannelCode", "code", "Code"]
        )

        name = first_nonempty(
            item,
            [
                "channelName",
                "ChannelName",
                "name",
                "Name",
                "displayName",
                "DisplayName"
            ]
        )

        if not code or not name:
            continue

        icon = first_nonempty(
            item,
            [
                "channelImage",
                "ChannelImage",
                "image",
                "Image",
                "channelIcon",
                "ChannelIcon"
            ]
        )

        channels[code] = {
            "name": name,
            "icon": icon
        }

    return channels


def normalize_programs(data):
    return get_list(data, [
        "prevueListViewModel",
        "PrevueListViewModel",
        "mobilPrevueViewModel",
        "MobilPrevueViewModel",
        "programs",
        "Programs",
    ])


def parse_datetime(value):
    value = clean_text(value)

    if not value:
        return None

    formats = (
        "%Y.%m.%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
    )

    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=TIMEZONE)

        except ValueError:
            continue

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TIMEZONE)

        return parsed.astimezone(TIMEZONE)

    except ValueError:
        return None


def build_xml_bytes(channels, programs):
    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL,
        }
    )

    for code in sorted(
        channels,
        key=lambda item: channels[item]["name"].casefold()
    ):
        info = channels[code]

        channel = ET.SubElement(
            root,
            "channel",
            {"id": code}
        )

        add_text(
            channel,
            "display-name",
            info["name"],
            "tr"
        )

        icon = info.get("icon", "")

        if icon.startswith("http://") or icon.startswith("https://"):
            ET.SubElement(
                channel,
                "icon",
                {"src": icon}
            )

    for program in sorted(
        programs,
        key=lambda item: (
            item["begin_dt"],
            item["channel_code"]
        )
    ):
        node = ET.SubElement(
            root,
            "programme",
            {
                "start": program["begin_dt"].strftime("%Y%m%d%H%M%S %z"),
                "stop": program["end_dt"].strftime("%Y%m%d%H%M%S %z"),
                "channel": program["channel_code"],
            }
        )

        add_text(
            node,
            "title",
            program["title"],
            "tr"
        )

        if program.get("description"):
            add_text(
                node,
                "desc",
                program["description"],
                "tr"
            )

        if program.get("genre"):
            add_text(
                node,
                "category",
                program["genre"],
                "tr"
            )

        if program.get("rating"):
            add_text(
                node,
                "rating",
                program["rating"]
            )

    ET.indent(root, space="  ")

    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True
    )


def add_text(parent, tag, value, lang=None):
    attrs = {"lang": lang} if lang else {}

    node = ET.SubElement(
        parent,
        tag,
        attrs
    )

    node.text = clean_text(value)

    return node


def merge_channel(channels, code, name, icon=""):
    name = clean_text(name)

    if not code or not name:
        return

    existing = channels.get(code)

    if existing is None:
        channels[code] = {
            "name": name,
            "icon": clean_text(icon)
        }
        return

    if (
        not existing.get("name")
        or existing["name"] == code
    ) and name != code:
        existing["name"] = name

    if not existing.get("icon") and icon:
        existing["icon"] = clean_text(icon)


def main():
    started = time.time()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("Kanal koduyla doğrudan eşleştirme")
    print("=" * 68)

    page_html = get_main_page()

    csrf_token, column_code, search_value, date_options = (
        extract_page_data(page_html)
    )

    target_dates = build_target_dates(date_options)

    print(f"Tarih seçeneği: {len(date_options)}")
    print(f"Alınacak gün: {len(target_dates)}")
    print("Kanal ve programlar servis üzerinden alınacak.")

    channels = {}
    all_programs = {}
    successful_days = 0
    total_received = 0

    for index, date_info in enumerate(target_dates, 1):
        begin_date = date_info["begin"]
        end_date = date_info["end"]

        label_date = parse_tivibu_date(begin_date)

        label = (
            label_date.strftime("%d.%m.%Y")
            if label_date
            else begin_date
        )

        print(
            f"[{index}/{DAYS}] {label} alınıyor...",
            end=" ",
            flush=True
        )

        data = post_multi_prevue(
            csrf_token,
            column_code,
            search_value,
            begin_date,
            end_date,
        )

        day_channels = normalize_channels(data)
        day_programs = normalize_programs(data)

        if not day_channels and not day_programs:
            keys = ", ".join(
                str(key)
                for key in list(data.keys())[:25]
            )

            details = describe_service_fields(data)

            raise RuntimeError(
                f"{label} için kanal/program listeleri çözümlenemedi. "
                f"Yanıt alanları: {keys or 'yok'}.\n"
                f"Servis alanlarının içeriği:\n{details}\n"
                "Mevcut XML korunuyor."
            )

        for code, info in day_channels.items():
            merge_channel(
                channels,
                code,
                info.get("name", ""),
                info.get("icon", "")
            )

        added = 0

        for item in day_programs:
            code = first_nonempty(
                item,
                ["channelCode", "ChannelCode"]
            )

            begin_value = first_nonempty(
                item,
                [
                    "beginTime",
                    "BeginTime",
                    "startTime",
                    "StartTime"
                ]
            )

            end_value = first_nonempty(
                item,
                [
                    "endTime",
                    "EndTime",
                    "stopTime",
                    "StopTime"
                ]
            )

            title = first_nonempty(
                item,
                [
                    "prevueName",
                    "PrevueName",
                    "name",
                    "Name",
                    "title",
                    "Title"
                ]
            )

            begin_dt = parse_datetime(begin_value)
            end_dt = parse_datetime(end_value)

            if (
                not code
                or not begin_dt
                or not end_dt
                or not title
                or end_dt <= begin_dt
            ):
                continue

            if code not in channels:
                fallback_name = first_nonempty(
                    item,
                    ["channelName", "ChannelName"]
                )

                merge_channel(
                    channels,
                    code,
                    fallback_name
                )

            if code not in channels:
                continue

            key = (
                code,
                begin_dt,
                end_dt,
                title.casefold()
            )

            if key in all_programs:
                continue

            all_programs[key] = {
                "channel_code": code,
                "begin_dt": begin_dt,
                "end_dt": end_dt,
                "title": title,
                "description": first_nonempty(
                    item,
                    [
                        "description",
                        "Description",
                        "desc",
                        "Desc"
                    ]
                ),
                "genre": first_nonempty(
                    item,
                    [
                        "genre",
                        "Genre",
                        "category",
                        "Category"
                    ]
                ),
                "rating": first_nonempty(
                    item,
                    [
                        "ratingId",
                        "RatingId",
                        "rating",
                        "Rating"
                    ]
                ),
            }

            added += 1

        total_received += len(day_programs)
        successful_days += 1

        print(
            f"{len(day_channels)} kanal, "
            f"{len(day_programs)} ham kayıt, "
            f"{added} yeni program"
        )

        time.sleep(REQUEST_DELAY)

    programs = list(all_programs.values())

    named_channels = {
        code: info
        for code, info in channels.items()
        if info.get("name") and info["name"] != code
    }

    if successful_days != DAYS:
        raise RuntimeError(
            f"Yalnızca {successful_days}/{DAYS} gün alındı. "
            "Mevcut XML korunuyor."
        )

    if len(named_channels) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(named_channels)} isimli kanal bulundu; "
            f"en az {MIN_CHANNELS} bekleniyordu. "
            "Mevcut XML korunuyor."
        )

    if len(programs) < MIN_PROGRAMS:
        raise RuntimeError(
            f"Yalnızca {len(programs)} program bulundu; "
            f"en az {MIN_PROGRAMS} bekleniyordu. "
            "Mevcut XML korunuyor."
        )

    days_found = sorted({
        item["begin_dt"].astimezone(TIMEZONE).date()
        for item in programs
    })

    if len(days_found) < 6:
        raise RuntimeError(
            f"Yalnızca {len(days_found)} farklı yayın günü bulundu; "
            "7 günlük veri doğrulanamadı. "
            "Mevcut XML korunuyor."
        )

    channel_codes = set(named_channels)

    invalid = [
        item
        for item in programs
        if item["channel_code"] not in channel_codes
    ]

    if invalid:
        raise RuntimeError(
            f"{len(invalid)} programın kanal eşleşmesi geçersiz. "
            "Mevcut XML korunuyor."
        )

    xml_bytes = build_xml_bytes(
        named_channels,
        programs
    )

    temp_file = OUTPUT_FILE + ".tmp"

    with open(temp_file, "wb") as file:
        file.write(xml_bytes)
        file.flush()
        os.fsync(file.fileno())

    os.replace(
        temp_file,
        OUTPUT_FILE
    )

    print("\n" + "=" * 68)
    print("7 GÜNLÜK EPG OLUŞTURULDU")
    print(f"Başarılı gün: {successful_days}/{DAYS}")
    print(f"Toplam kanal: {len(named_channels)}")
    print(f"Toplam program: {len(programs)}")
    print(f"Ham servis kaydı: {total_received}")
    print(f"Program bulunan gün: {len(days_found)}")

    if days_found:
        print(
            f"İlk gün: {days_found[0].strftime('%d.%m.%Y')}"
        )

        print(
            f"Son gün: {days_found[-1].strftime('%d.%m.%Y')}"
        )

    print(f"Dosya: {OUTPUT_FILE}")
    print(f"Süre: {time.time() - started:.1f} saniye")
    print("=" * 68)


if __name__ == "__main__":
    main()
