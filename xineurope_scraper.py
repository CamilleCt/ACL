#!/usr/bin/env python3
"""新欧洲·跳蚤（bbs.xineurope.com）房屋租赁抓取。

列表通过 Discuz 插件 xigua_hb 的 AJAX 接口 ac=list_item 返回 XML(CDATA 包 HTML 卡片)，
并支持服务端关键词过滤 keyword=，所以可以直接按 arcueil/cachan/laplace 精准取数。
"""
import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from huarenjie_rent_scraper import Listing

BASE = "https://bbs.xineurope.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) huarenjie-rent-filter/2.0"
CAT_ID = 2  # 房屋租赁

# 站点 -> 在新欧洲里可被关键词搜索命中的法文地名
STATION_KEYWORDS = {
    "Cite Universitaire": ["cité universitaire", "universitaire"],
    "Gentilly": ["gentilly"],
    "Laplace": ["laplace"],
    "Arcueil-Cachan": ["arcueil", "cachan"],
    "Bagneux": ["bagneux"],
    "Bourg-la-Reine": ["bourg-la-reine"],
    "Parc de Sceaux": ["sceaux"],
}

CARD_RE = re.compile(
    r'<a class="card_row[^"]*"[^>]*data-id="(\d+)"[^>]*href="(xinxi_\d+\.html)"[^>]*>(.*?)</a>',
    re.S,
)


def fetch(url: str, timeout: int = 20, tries: int = 2, delay: float = 1.0) -> str:
    last_error = None
    for attempt in range(tries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE}/cat_2.html",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(delay * (attempt + 1))
    raise RuntimeError(f"xineurope fetch failed: {url}: {last_error}")


def strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def list_url(keyword: str, page: int, pagesize: int = 20) -> str:
    kw = urllib.parse.quote(keyword)
    return (
        f"{BASE}/plugin.php?id=xigua_hb&cat_id={CAT_ID}&province=&city=&dist="
        f"&orderby=&keyword={kw}&filter=&ac=list_item&inajax=1&pagesize={pagesize}&page={page}"
    )


def detail_url(info_id: str) -> str:
    return f"{BASE}/xinxi_{info_id}.html"


def parse_cards(xml: str) -> list[dict]:
    cards = []
    for info_id, href, inner in CARD_RE.findall(xml):
        h3 = re.search(r"<h3>(.*?)</h3>", inner, re.S)
        title_full = strip_tags(h3.group(1)) if h3 else ""
        ci = re.search(r'<div class="car_info">(.*?)</div>', inner, re.S)
        rent, area, deposit = "", "", ""
        if ci:
            segs = [strip_tags(s) for s in re.split(r"<em>\s*/\s*</em>", ci.group(1))]
            segs = [s for s in segs if s]
            if len(segs) > 0:
                rent = segs[0]
            if len(segs) > 1:
                area = segs[1]
            if len(segs) > 2:
                deposit = segs[2]
        cards.append(
            {
                "id": info_id,
                "title": title_full,
                "rent": rent,
                "area": area,
                "deposit": deposit,
            }
        )
    return cards


def detail_text(info_id: str) -> tuple[str, str]:
    """一次请求取详情页的 (发布日期, 正文)；失败返回空串（不阻断流程）。

    正文在 <div class="job-detail"> 内，合租/分租等关键信号常只出现在这里，
    必须并入文本供过滤判断。
    """
    try:
        page_html = fetch(detail_url(info_id))
    except RuntimeError:
        return "", ""
    md = re.search(r"发布时间\s*<span>\s*(\d{4}-\d{2}-\d{2})", page_html)
    date = md.group(1) if md else ""
    mb = re.search(r'<div class="job-detail">(.*?)</div>', page_html, re.S)
    body = strip_tags(mb.group(1)) if mb else ""
    return date, body


def detect_source(text: str) -> str:
    if "中介" in text:
        return "中介房源"
    if "房东直租" in text or "个人房东" in text or "直接房东" in text:
        return "直接房东"
    return ""


def detect_region(text: str) -> str:
    m = re.search(r"(大巴黎|小巴黎)?\s*(\d{2,3}\s*省)?\s*([A-Za-zÀ-ÿ][\w\-’' ]{2,30})", text)
    return strip_tags(m.group(0)) if m else ""


def card_to_listing(card: dict, with_detail: bool = True) -> Listing:
    info_id = card["id"]
    title_full = card["title"]
    updated, body = detail_text(info_id) if with_detail else ("", "")
    excerpt = (title_full + ("  " + body if body else ""))[:600]
    return Listing(
        id="xe_" + info_id,
        url=detail_url(info_id),
        catid=CAT_ID,
        category="房屋租赁",
        title=title_full[:80],
        updated=updated,
        rent=card.get("rent", ""),
        area=card.get("area", ""),
        source=detect_source(title_full),
        region=detect_region(title_full),
        phone_masked="",
        phones_in_text="",
        score=0,
        matched_terms="",
        excerpt=excerpt,
        platform="新欧洲",
    )


def keywords_for(selected_stations: list[str], extra: list[str]) -> list[str]:
    kws: list[str] = []
    for station in selected_stations:
        kws.extend(STATION_KEYWORDS.get(station, []))
    kws.extend(extra)
    # 去重保序
    return list(dict.fromkeys(k.strip() for k in kws if k and k.strip()))


def candidate_cards(keywords: list[str], max_pages: int = 20, pagesize: int = 20):
    """对每个关键词翻页拉取卡片，跨关键词去重后逐个 yield。"""
    seen: set[str] = set()
    for keyword in keywords:
        for page in range(1, int(max_pages) + 1):
            try:
                xml = fetch(list_url(keyword, page, pagesize))
            except RuntimeError as exc:
                raise RuntimeError(f"keyword={keyword} page={page}: {exc}")
            cards = parse_cards(xml)
            if not cards:
                break
            for card in cards:
                if card["id"] in seen:
                    continue
                seen.add(card["id"])
                yield card
            if len(cards) < pagesize:
                break
            time.sleep(0.4)
