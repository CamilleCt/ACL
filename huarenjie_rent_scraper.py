#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict


BASE = "https://www.huarenjiewang.com"
CITY = "faguo"
UA = "Mozilla/5.0 (compatible; huarenjie-rent-filter/1.0)"

TARGET_TERMS = [
    "arcueil-cachan",
    "arcueil cachan",
    "arcueil",
    "cachan",
    "laplace",
    "bagneux",
    "rer b",
    "rerb",
    "b线",
    "快线b",
]

LOCAL_TERMS = [
    "arcueil-cachan",
    "arcueil cachan",
    "arcueil",
    "cachan",
    "laplace",
    "bagneux",
    "94110",
    "94230",
    "92220",
]

FAR_NEGATIVE_TERMS = [
    "antony",
    "la courneuve",
    "lacourneuve",
    "drancy",
    "aubervilliers",
    "roissy",
    "mitry",
    "sevran",
    "aulnay",
    "saint-denis",
    "st denis",
    "gare du nord",
    "creteil",
    "créteil",
    "villejuif",
    "ivry",
    "romainville",
]

RENTAL_CATIDS = {
    32: "房屋出租",
    33: "留学生房源",
}


@dataclass
class Listing:
    id: str
    url: str
    catid: int
    category: str
    title: str
    updated: str
    rent: str
    area: str
    source: str
    region: str
    phone_masked: str
    phones_in_text: str
    score: int
    matched_terms: str
    excerpt: str
    platform: str = "华人街"


def fetch(url: str, timeout: int = 12, tries: int = 2, delay: float = 1.0) -> str:
    last_error = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(delay * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url}: {last_error}")


def strip_tags(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def list_url(catid: int, page: int) -> str:
    if page == 1:
        return f"{BASE}/city/{CITY}/category-catid-{catid}.html"
    return f"{BASE}/city/{CITY}/category-catid-{catid}-page-{page}.html"


def detail_url(info_id: str) -> str:
    return f"{BASE}/city/{CITY}/information-id-{info_id}.html"


def parse_list_items(page_html: str) -> list[tuple[str, str]]:
    items = re.findall(
        r'<a href="https://www\.huarenjiewang\.com/city/faguo/information-id-(\d+)\.html"[^>]*class="shenghuo-item-link"[^>]*>(.*?)</a>',
        page_html,
        flags=re.S,
    )
    seen = set()
    out = []
    for info_id, title in items:
        if info_id not in seen:
            seen.add(info_id)
            out.append((info_id, strip_tags(title)))
    return out


def parse_field(page_html: str, label: str) -> str:
    m = re.search(rf"<span>{re.escape(label)}：</span>(.*?)</li>", page_html, flags=re.S)
    if not m:
        return ""
    return strip_tags(m.group(1))


def parse_title(page_html: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, flags=re.S)
    if m:
        return strip_tags(m.group(1))
    m = re.search(r"<title>(.*?)</title>", page_html, flags=re.S)
    return strip_tags(m.group(1)).replace("-法国华人街分类广告", "") if m else ""


def parse_body(page_html: str) -> str:
    m = re.search(r'<div class="content"[^>]*>(.*?)<div class="contact_me">', page_html, flags=re.S)
    if m:
        return strip_tags(m.group(1))
    m = re.search(r'<meta name="description" content="([^"]*)"', page_html, flags=re.S)
    return html.unescape(m.group(1)).strip() if m else strip_tags(page_html)


def parse_update_date(info_id: str) -> str:
    js_url = f"{BASE}/javascript.php?part=information_time&id={info_id}"
    text = fetch(js_url, timeout=15, tries=2, delay=0.8)
    m = re.search(r'["\'](\d{4}-\d{2}-\d{2})["\']', text)
    if m:
        return m.group(1)
    m = re.search(r'["\']([^"\']+)["\']', text)
    if not m:
        return ""
    value = m.group(1)
    today = dt.date.today()
    if "前天" in value:
        return (today - dt.timedelta(days=2)).isoformat()
    if "昨天" in value:
        return (today - dt.timedelta(days=1)).isoformat()
    if any(token in value for token in ["分钟前", "小时前", "刚刚"]):
        return today.isoformat()
    return ""


def score_text(text: str) -> tuple[int, list[str]]:
    folded = text.lower()
    matched = [term for term in TARGET_TERMS if term in folded]
    score = len(matched)
    if "arcueil" in matched or "cachan" in matched or "arcueil-cachan" in matched:
        score += 4
    if "laplace" in matched or "bagneux" in matched:
        score += 3
    if "rer b" in matched or "rerb" in matched or "b线" in matched or "快线b" in matched:
        score += 1
    if any(term in folded for term in FAR_NEGATIVE_TERMS):
        score -= 4
    return score, matched


def has_local_term(text: str) -> bool:
    folded = text.lower()
    return any(term in folded for term in LOCAL_TERMS)


def parse_listing(info_id: str, catid: int) -> Listing:
    url = detail_url(info_id)
    page_html = fetch(url)
    title = parse_title(page_html)
    body = parse_body(page_html)
    try:
        updated = parse_update_date(info_id)
    except RuntimeError:
        updated = ""
    rent = parse_field(page_html, "租金")
    area = parse_field(page_html, "面积")
    source = parse_field(page_html, "来源")
    region = parse_field(page_html, "区域")
    phone_masked = ""
    m = re.search(r'<font class="tel red">(.*?)</font>', page_html, flags=re.S)
    if m:
        phone_masked = strip_tags(m.group(1))
    phones = sorted(set(re.findall(r"\b0[1-9](?:[\s.-]?\d{2}){4}\b", body)))
    combined = f"{title} {body}"
    score, matched = score_text(combined)
    return Listing(
        id=info_id,
        url=url,
        catid=catid,
        category=RENTAL_CATIDS.get(catid, str(catid)),
        title=title,
        updated=updated,
        rent=rent,
        area=area,
        source=source,
        region=region,
        phone_masked=phone_masked,
        phones_in_text=", ".join(phones),
        score=score,
        matched_terms=", ".join(matched),
        excerpt=body[:260],
    )


def within_cutoff(updated: str, cutoff: dt.date) -> bool:
    if not updated:
        return True
    try:
        return dt.date.fromisoformat(updated) >= cutoff
    except ValueError:
        return True


def scrape(max_pages: int, months: int, min_score: int) -> list[Listing]:
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=months * 31)
    results: list[Listing] = []
    seen: set[str] = set()

    for catid in RENTAL_CATIDS:
        for page in range(1, max_pages + 1):
            print(f"scan catid={catid} page={page}", flush=True)
            try:
                page_items = parse_list_items(fetch(list_url(catid, page)))
            except RuntimeError as exc:
                print(f"  skip page: {exc}", flush=True)
                continue
            print(f"  found {len(page_items)} ids", flush=True)
            if not page_items:
                break
            for info_id, list_title in page_items:
                if info_id in seen:
                    continue
                if not has_local_term(list_title):
                    continue
                seen.add(info_id)
                try:
                    listing = parse_listing(info_id, catid)
                except RuntimeError as exc:
                    print(f"  skip id={info_id}: {exc}", flush=True)
                    continue
                if has_local_term(f"{listing.title} {listing.excerpt}") and listing.score >= min_score and within_cutoff(listing.updated, cutoff):
                    print(f"  match id={info_id} score={listing.score} updated={listing.updated}", flush=True)
                    results.append(listing)
                time.sleep(0.35)
            time.sleep(0.8)

    results.sort(key=lambda item: (item.score, item.updated), reverse=True)
    return results


def write_outputs(listings: list[Listing], prefix: str) -> None:
    with open(f"{prefix}.json", "w", encoding="utf-8") as f:
        json.dump([asdict(item) for item in listings], f, ensure_ascii=False, indent=2)
    with open(f"{prefix}.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(listings[0]).keys()) if listings else list(Listing.__dataclass_fields__.keys()))
        writer.writeheader()
        for item in listings:
            writer.writerow(asdict(item))


def main() -> None:
    parser = argparse.ArgumentParser(description="Filter Huarenjie France rental listings near RER B Arcueil-Cachan.")
    parser.add_argument("--max-pages", type=int, default=20, help="pages per category to scan")
    parser.add_argument("--months", type=int, default=3, help="only keep listings updated in the last N months")
    parser.add_argument("--min-score", type=int, default=2, help="minimum location score to keep")
    parser.add_argument("--output", default="huarenjie_rentals_arcueil_cachan", help="output file prefix")
    args = parser.parse_args()

    listings = scrape(args.max_pages, args.months, args.min_score)
    write_outputs(listings, args.output)
    for item in listings:
        print(f"[{item.score}] {item.updated} {item.title}")
        print(f"    {item.rent} {item.area} {item.region} {item.url}")
        print(f"    matched: {item.matched_terms}")


if __name__ == "__main__":
    main()
