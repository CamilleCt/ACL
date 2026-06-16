#!/usr/bin/env python3
import datetime as dt
import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import huarenjie_rent_scraper as hrj
import xineurope_scraper as xe
import notifier


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "huarenjie_rentals.sqlite3"
HOST = "127.0.0.1"
PORT = 8910  # 8765 落入 Windows 系统保留端口区间(8762-8861)，改用 8910

SOURCES = {
    "huarenjie": "华人街",
    "xineurope": "新欧洲",
}

B_LINE_TERMS = {
    "Cite Universitaire": ["cité universitaire", "cite universitaire", "大学城"],
    "Gentilly": ["gentilly", "94250"],
    "Laplace": ["laplace", "拉普拉斯"],
    "Arcueil-Cachan": ["arcueil-cachan", "arcueil cachan", "arcueil", "cachan", "94110", "94230"],
    "Bagneux": ["bagneux", "92220", "lucie aubrac"],
    "Bourg-la-Reine": ["bourg-la-reine", "bourg la reine", "92340"],
    "Parc de Sceaux": ["parc de sceaux", "sceaux", "92330"],
}

DEFAULT_CONFIG = {
    "max_pages": 20,
    "months": 3,
    "max_rent": 1000,
    "whole_only": True,
    "selected_stations": ["Arcueil-Cachan", "Laplace"],
    "include_extra": "",
    "exclude_extra": "",
    "sources": ["huarenjie", "xineurope"],
    "scan_interval_min": 10,
    "auto_scan": False,
    "notify_enabled": True,
    "notify_channel": "email",        # email | pushplus
    "smtp_user": "chentongfrance@gmail.com",
    "smtp_app_password": "",
    "email_to": "chentongfrance@gmail.com",
    "pushplus_token": "",
    "seeded": False,
}

# UI 可写入的键（seeded 为内部状态，不允许前端覆盖）
EDITABLE_KEYS = [k for k in DEFAULT_CONFIG if k != "seeded"]

WHOLE_INCLUDE = [
    "整租", "一房一厅", "两房一厅", "三房一厅", "一室一厅", "二室一厅", "两室一厅",
    "三室一厅", "一房", "两房", "二房", "三房", "studio", "studette", "f1", "f2",
    "f3", "t1", "t2", "t3", "独立studio", "独立公寓", "独立房子", "整套", "整栋",
    "整棟", "别墅", "別墅", "公寓",
]

SHARE_EXCLUDE = [
    "合租", "分租", "单间", "单人间", "床位", "次卧", "主卧", "房间出租", "出租一间",
    "出租房间", "一间出租", "其中一间", "其中一个房间", "中的一房间", "中的一间",
    "招室友", "室友", "一个房间", "一间房", "共用", "colocation", "colocataire",
    "coloc", "chambre", "sous-location",
]

SHARE_REGEXES = [
    re.compile(r"[一二两三四五\d]\s*(?:室|房)[^，。；,;]{0,6}(?:中的|其中)"),
    re.compile(r"(?:出租|招)[^，。；,;]{0,4}(?:一|1)?\s*(?:间|个)\s*(?:房间|卧|床位)"),
]


state_lock = threading.Lock()
scan_state = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "message": "idle",
    "inserted": 0,
    "matched": 0,
    "errors": [],
    "last_notify": "",
}

_last_auto_run = 0.0


# --------------------------------------------------------------------------- DB
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            create table if not exists listings (
                id text primary key,
                platform text,
                url text not null,
                catid integer not null,
                category text not null,
                title text not null,
                updated text,
                rent text,
                rent_num integer,
                area text,
                area_num integer,
                source text,
                region text,
                phone_masked text,
                phones_in_text text,
                score integer not null,
                matched_terms text,
                matched_stations text,
                is_whole integer not null,
                is_share integer not null,
                excerpt text,
                first_seen text not null,
                last_seen text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists config (
                key text primary key,
                value text not null
            )
            """
        )
        # 迁移：老库可能缺少 platform 列
        cols = {row["name"] for row in conn.execute("pragma table_info(listings)")}
        if "platform" not in cols:
            conn.execute("alter table listings add column platform text")
            # 老库的历史房源都来自华人街，回填来源
            conn.execute("update listings set platform = '华人街' where platform is null")
        for key, value in DEFAULT_CONFIG.items():
            conn.execute(
                "insert or ignore into config (key, value) values (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )


def get_config() -> dict:
    init_db()
    cfg = DEFAULT_CONFIG.copy()
    with db() as conn:
        rows = conn.execute("select key, value from config").fetchall()
    for row in rows:
        try:
            cfg[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            cfg[row["key"]] = row["value"]
    return cfg


def set_config(payload: dict) -> dict:
    cfg = get_config()
    for key in EDITABLE_KEYS:
        if key in payload:
            cfg[key] = payload[key]
    cfg["max_pages"] = max(1, min(300, int(cfg["max_pages"])))
    cfg["months"] = max(1, min(12, int(cfg["months"])))
    cfg["max_rent"] = max(100, min(5000, int(cfg["max_rent"])))
    cfg["scan_interval_min"] = max(2, min(240, int(cfg["scan_interval_min"])))
    cfg["whole_only"] = bool(cfg["whole_only"])
    cfg["auto_scan"] = bool(cfg["auto_scan"])
    cfg["notify_enabled"] = bool(cfg["notify_enabled"])
    cfg["notify_channel"] = cfg.get("notify_channel") if cfg.get("notify_channel") in ("email", "pushplus") else "email"
    cfg["smtp_user"] = str(cfg.get("smtp_user", "")).strip()
    cfg["smtp_app_password"] = str(cfg.get("smtp_app_password", "")).strip()
    cfg["email_to"] = str(cfg.get("email_to", "")).strip()
    cfg["pushplus_token"] = str(cfg.get("pushplus_token", "")).strip()
    cfg["sources"] = [s for s in cfg["sources"] if s in SOURCES] or list(SOURCES)
    cfg["selected_stations"] = [
        station for station in cfg["selected_stations"] if station in B_LINE_TERMS
    ] or DEFAULT_CONFIG["selected_stations"]
    with db() as conn:
        for key, value in cfg.items():
            conn.execute(
                "insert or replace into config (key, value) values (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
    return cfg


def set_seeded(value: bool = True) -> None:
    with db() as conn:
        conn.execute(
            "insert or replace into config (key, value) values (?, ?)",
            ("seeded", json.dumps(bool(value))),
        )


# ---------------------------------------------------------------------- filters
def split_terms(value: str) -> list[str]:
    return [part.strip().lower() for part in re.split(r"[,，\n]", value or "") if part.strip()]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def parse_int(value: str) -> int | None:
    if not value:
        return None
    m = re.search(r"(\d+)", value.replace(" ", ""))
    return int(m.group(1)) if m else None


def parse_rent_num(listing: hrj.Listing) -> int | None:
    n = parse_int(listing.rent)
    if n:
        return n
    text = f"{listing.title} {listing.excerpt}"
    cands = []
    for m in re.finditer(r"(\d{2,4})\s*(?:€|euros?|欧元?|eur)", text, flags=re.I):
        v = int(m.group(1))
        if 100 <= v <= 5000:
            cands.append(v)
    return min(cands) if cands else None


def station_terms(config: dict) -> list[str]:
    terms: list[str] = []
    for station in config["selected_stations"]:
        terms.extend(B_LINE_TERMS.get(station, []))
    terms.extend(split_terms(config.get("include_extra", "")))
    return sorted(set(term.lower() for term in terms))


def matched_stations(text: str, config: dict) -> list[str]:
    folded = normalize(text)
    matched = []
    for station in config["selected_stations"]:
        if any(term.lower() in folded for term in B_LINE_TERMS.get(station, [])):
            matched.append(station)
    extra = [term for term in split_terms(config.get("include_extra", "")) if term in folded]
    return matched + extra


def looks_share(text: str, config: dict) -> bool:
    folded = normalize(text)
    extra = split_terms(config.get("exclude_extra", ""))
    if any(term in folded for term in SHARE_EXCLUDE + extra):
        return True
    return any(rx.search(folded) for rx in SHARE_REGEXES)


def is_room_sublet(text: str, area_num: int | None) -> bool:
    """多房型却面积过小 => 实为整套中的一间（分租）。真正的两/三室一厅不会 <30㎡。"""
    folded = normalize(text)
    rooms = 0
    if re.search(r"[三3]\s*室|三房|三居|\bt3\b|\bf3\b", folded):
        rooms = 3
    elif re.search(r"[两二2]\s*室|两房|二房|两居|\bt2\b|\bf2\b", folded):
        rooms = 2
    return rooms >= 2 and bool(area_num) and area_num < 30


def share_listing(listing: hrj.Listing, config: dict) -> bool:
    text = f"{listing.title} {listing.excerpt}"
    if looks_share(text, config):
        return True
    return is_room_sublet(text, parse_int(listing.area))


def looks_whole(text: str) -> bool:
    folded = normalize(text)
    if any(term in folded for term in WHOLE_INCLUDE):
        return True
    if re.search(r"\b[ft][1-5]\b", folded):
        return True
    return False


def relevant_list_title(title: str, config: dict) -> bool:
    folded = normalize(title)
    return any(term in folded for term in station_terms(config))


def rent_ok(listing: hrj.Listing, config: dict) -> bool:
    n = parse_rent_num(listing)
    if not n:
        return True  # 价格未知，不排除（界面会标“价格待确认”）
    return n <= int(config.get("max_rent", 1000))


def relevant_detail(listing: hrj.Listing, config: dict) -> bool:
    text = f"{listing.title} {listing.excerpt}"
    if not matched_stations(text, config):
        return False
    if config.get("whole_only") and share_listing(listing, config):
        return False
    if config.get("whole_only") and not looks_whole(text):
        return False
    if not rent_ok(listing, config):
        return False
    return True


def save_listing(listing: hrj.Listing, config: dict) -> bool:
    """写入/更新一条房源，返回是否为新房源（之前不存在）。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    data = asdict(listing)
    text = f"{listing.title} {listing.excerpt}"
    data["rent_num"] = parse_rent_num(listing)
    data["area_num"] = parse_int(listing.area)
    data["matched_stations"] = ", ".join(matched_stations(text, config))
    data["is_share"] = 1 if share_listing(listing, config) else 0
    data["is_whole"] = 1 if looks_whole(text) and not data["is_share"] else 0
    with db() as conn:
        existing = conn.execute(
            "select first_seen from listings where id = ?", (listing.id,)
        ).fetchone()
        is_new = existing is None
        first_seen = existing["first_seen"] if existing else now
        conn.execute(
            """
            insert or replace into listings (
                id, platform, url, catid, category, title, updated, rent, rent_num, area, area_num,
                source, region, phone_masked, phones_in_text, score, matched_terms,
                matched_stations, is_whole, is_share, excerpt, first_seen, last_seen
            ) values (
                :id, :platform, :url, :catid, :category, :title, :updated, :rent, :rent_num, :area, :area_num,
                :source, :region, :phone_masked, :phones_in_text, :score, :matched_terms,
                :matched_stations, :is_whole, :is_share, :excerpt, :first_seen, :last_seen
            )
            """,
            {**data, "first_seen": first_seen, "last_seen": now},
        )
    return is_new


# ----------------------------------------------------------------------- scan
def update_state(**kwargs) -> None:
    with state_lock:
        scan_state.update(kwargs)


def scan_huarenjie(config: dict, process, errors: list[str]) -> None:
    for catid in hrj.RENTAL_CATIDS:
        for page in range(1, int(config["max_pages"]) + 1):
            update_state(message=f"华人街 catid={catid} page={page}")
            try:
                items = hrj.parse_list_items(hrj.fetch(hrj.list_url(catid, page)))
            except RuntimeError as exc:
                errors.append(str(exc))
                update_state(errors=errors[-8:])
                continue
            if not items:
                break
            for info_id, list_title in items:
                if not relevant_list_title(list_title, config):
                    continue
                try:
                    listing = hrj.parse_listing(info_id, catid)
                except RuntimeError as exc:
                    errors.append(str(exc))
                    update_state(errors=errors[-8:])
                    continue
                process(listing)
                time.sleep(0.25)
            time.sleep(0.6)


def scan_xineurope(config: dict, process, errors: list[str]) -> None:
    keywords = xe.keywords_for(
        config["selected_stations"], split_terms(config.get("include_extra", ""))
    )
    if not keywords:
        return
    try:
        for card in xe.candidate_cards(keywords, max_pages=int(config["max_pages"])):
            update_state(message=f"新欧洲 xinxi_{card['id']}")
            try:
                listing = xe.card_to_listing(card)
            except RuntimeError as exc:
                errors.append(str(exc))
                update_state(errors=errors[-8:])
                continue
            process(listing)
            time.sleep(0.2)
    except RuntimeError as exc:
        errors.append(str(exc))
        update_state(errors=errors[-8:])


def run_scan(config: dict, notify: bool = True) -> list[hrj.Listing]:
    update_state(
        running=True,
        started_at=dt.datetime.now().isoformat(timespec="seconds"),
        finished_at="",
        message="starting",
        inserted=0,
        matched=0,
        errors=[],
    )
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=int(config["months"]) * 31)
    seeded = bool(get_config().get("seeded"))
    seen: set[str] = set()
    stats = {"inserted": 0, "matched": 0}
    new_listings: list[hrj.Listing] = []
    errors: list[str] = []

    def process(listing: hrj.Listing) -> None:
        if listing.id in seen:
            return
        seen.add(listing.id)
        if not hrj.within_cutoff(listing.updated, cutoff):
            return
        if not relevant_detail(listing, config):
            return
        stats["matched"] += 1
        is_new = save_listing(listing, config)
        if is_new:
            stats["inserted"] += 1
            new_listings.append(listing)
        update_state(message=f"命中 {listing.id}", inserted=stats["inserted"], matched=stats["matched"])

    try:
        if "huarenjie" in config["sources"]:
            scan_huarenjie(config, process, errors)
        if "xineurope" in config["sources"]:
            scan_xineurope(config, process, errors)
    finally:
        if not seeded:
            set_seeded(True)
        update_state(
            running=False,
            finished_at=dt.datetime.now().isoformat(timespec="seconds"),
            message="完成" if seeded else "完成（首次为基线，不推送）",
            inserted=stats["inserted"],
            matched=stats["matched"],
            errors=errors[-8:],
        )

    if notify and seeded and new_listings:
        notify_new(new_listings, config)
    return new_listings


def send_notification(title: str, body_html: str, config: dict) -> tuple[bool, str]:
    """按配置的渠道发送通知，返回 (是否成功, 说明)。"""
    if config.get("notify_channel") == "pushplus":
        return notifier.push_pushplus(config.get("pushplus_token", ""), title, body_html)
    return notifier.send_email(
        config.get("smtp_user", ""),
        config.get("smtp_app_password", ""),
        config.get("email_to", ""),
        title,
        notifier.email_body(title, body_html),
    )


def notify_new(listings: list[hrj.Listing], config: dict) -> None:
    if not config.get("notify_enabled") or not listings:
        return
    title = f"🏠 {len(listings)} 条新房源 · Arcueil/Cachan/Laplace ≤{config.get('max_rent', 1000)}€"
    ok, msg = send_notification(title, notifier.build_html(listings), config)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    update_state(last_notify=f"{stamp} · {'已发送' if ok else '发送失败'}：{msg}")


def run_scan_async() -> None:
    cfg = get_config()
    threading.Thread(target=run_scan, args=(cfg,), kwargs={"notify": True}, daemon=True).start()


def scheduler_loop() -> None:
    global _last_auto_run
    while True:
        try:
            cfg = get_config()
            if cfg.get("auto_scan"):
                with state_lock:
                    running = scan_state["running"]
                interval = int(cfg.get("scan_interval_min", 10)) * 60
                if not running and (time.time() - _last_auto_run) >= interval:
                    _last_auto_run = time.time()
                    run_scan(cfg, notify=True)
        except Exception as exc:  # noqa: BLE001
            with state_lock:
                errs = (scan_state.get("errors", []) + [f"scheduler: {exc}"])[-8:]
            update_state(errors=errs)
        time.sleep(15)


# ---------------------------------------------------------------------- query
def listings(params: dict) -> list[dict]:
    clauses = []
    values: list[object] = []
    if params.get("whole_only", ["1"])[0] == "1":
        clauses.append("is_share = 0 and is_whole = 1")
    if params.get("station", [""])[0]:
        clauses.append("matched_stations like ?")
        values.append(f"%{params['station'][0]}%")
    if params.get("platform", [""])[0]:
        clauses.append("platform = ?")
        values.append(params["platform"][0])
    if params.get("q", [""])[0]:
        q = f"%{params['q'][0]}%"
        clauses.append("(title like ? or excerpt like ? or matched_terms like ?)")
        values.extend([q, q, q])
    # 始终遵守预算上限：超预算的不显示（价格未知的保留）
    clauses.append("(rent_num is null or rent_num <= ?)")
    values.append(int(get_config().get("max_rent", 1000)))
    where = "where " + " and ".join(clauses) if clauses else ""
    sql = f"select * from listings {where} order by updated desc, last_seen desc limit 500"
    with db() as conn:
        rows = conn.execute(sql, values).fetchall()
    return [dict(row) for row in rows]


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>华人街 + 新欧洲 租房监控</title>
  <style>
    :root { color-scheme: light; --ink:#202124; --muted:#5f6368; --line:#dadce0; --bg:#f8fafd; --panel:#fff; --accent:#0b57d0; --ok:#137333; --warn:#b3261e; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink); background:var(--bg); }
    header { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:18px 24px; border-bottom:1px solid var(--line); background:var(--panel); position:sticky; top:0; z-index:2; }
    h1 { font-size:20px; margin:0; }
    main { display:grid; grid-template-columns: 340px 1fr; gap:18px; padding:18px; max-width:1440px; margin:0 auto; }
    aside, .content { background:var(--panel); border:1px solid var(--line); border-radius:8px; }
    aside { padding:16px; height:fit-content; }
    .content { padding:0; overflow:hidden; }
    label { display:block; font-size:13px; color:var(--muted); margin:12px 0 6px; }
    input, textarea, select { width:100%; border:1px solid var(--line); border-radius:6px; padding:9px 10px; font-size:14px; background:#fff; color:var(--ink); }
    textarea { min-height:60px; resize:vertical; }
    .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .stations, .sources { display:grid; gap:8px; margin-top:8px; }
    .check { display:flex; align-items:center; gap:8px; font-size:14px; }
    .check input { width:auto; }
    .section { border-top:1px solid var(--line); margin-top:16px; padding-top:8px; }
    .section h3 { font-size:13px; color:var(--muted); margin:8px 0 0; font-weight:600; }
    .actions { display:flex; gap:8px; margin-top:14px; flex-wrap:wrap; }
    button { border:1px solid var(--accent); background:var(--accent); color:white; border-radius:6px; padding:9px 12px; font-size:14px; cursor:pointer; }
    button.secondary { background:white; color:var(--accent); }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .hint { font-size:12px; color:var(--muted); margin:6px 0 0; line-height:1.5; }
    .hint a { color:var(--accent); }
    .status { padding:12px 16px; border-bottom:1px solid var(--line); display:flex; align-items:center; justify-content:space-between; gap:12px; color:var(--muted); font-size:13px; flex-wrap:wrap; }
    .toolbar { display:grid; grid-template-columns: 1fr 150px 130px 120px; gap:10px; padding:14px 16px; border-bottom:1px solid var(--line); }
    .list { display:grid; }
    .item { display:grid; grid-template-columns: 1fr auto; gap:12px; padding:16px; border-bottom:1px solid var(--line); }
    .item h2 { margin:0 0 8px; font-size:16px; line-height:1.35; }
    .meta { display:flex; flex-wrap:wrap; gap:8px 12px; color:var(--muted); font-size:13px; }
    .excerpt { margin:10px 0 0; color:#3c4043; line-height:1.5; font-size:14px; max-width:960px; }
    .badge { display:inline-flex; align-items:center; min-height:22px; padding:2px 7px; border-radius:5px; border:1px solid var(--line); background:#fff; color:#3c4043; }
    .badge.hrj { border-color:#fdd; background:#fff5f5; color:#b3261e; }
    .badge.xe { border-color:#d6e4ff; background:#f3f7ff; color:#0b57d0; }
    .price { font-weight:700; font-size:16px; color:var(--ok); white-space:nowrap; }
    .empty { padding:38px 16px; text-align:center; color:var(--muted); }
    a { color:var(--accent); text-decoration:none; }
    @media (max-width: 900px) { main { grid-template-columns:1fr; padding:12px; } .toolbar { grid-template-columns:1fr; } .item { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>华人街 + 新欧洲 租房监控</h1>
    <div id="topStatus">准备就绪</div>
  </header>
  <main>
    <aside>
      <label>数据源</label>
      <div id="sources" class="sources"></div>

      <div class="grid2">
        <div><label>扫描页数 / 源</label><input id="maxPages" type="number" min="1" max="300" /></div>
        <div><label>房租上限 (€)</label><input id="maxRent" type="number" min="100" max="5000" /></div>
      </div>
      <div class="grid2">
        <div><label>更新范围（月）</label><input id="months" type="number" min="1" max="12" /></div>
        <div><label>扫描间隔（分钟）</label><input id="interval" type="number" min="2" max="240" /></div>
      </div>
      <label class="check" style="margin-top:12px"><input id="wholeOnly" type="checkbox" /> 只看整租，排除合租/分租/单间</label>

      <label>站点范围（B 线及附近）</label>
      <div id="stations" class="stations"></div>

      <label>额外包含关键词</label>
      <textarea id="includeExtra" placeholder="例如：robinson, sceaux"></textarea>
      <label>额外排除关键词</label>
      <textarea id="excludeExtra" placeholder="例如：短租, colocataire"></textarea>

      <div class="section">
        <h3>自动监控</h3>
        <label class="check" style="margin-top:10px"><input id="autoScan" type="checkbox" /> 开启后台自动扫描（按上面的间隔）</label>
      </div>

      <div class="section">
        <h3>提醒方式</h3>
        <label class="check" style="margin-top:10px"><input id="notifyEnabled" type="checkbox" /> 命中新房源时发提醒</label>
        <label>渠道</label>
        <select id="notifyChannel">
          <option value="email">邮箱 Gmail（免费）</option>
          <option value="pushplus">微信 PushPlus</option>
        </select>
        <div id="emailFields">
          <label>发件 Gmail 地址</label>
          <input id="smtpUser" type="text" placeholder="yourname@gmail.com" />
          <label>Gmail 应用专用密码（16 位）</label>
          <input id="smtpPass" type="text" placeholder="xxxx xxxx xxxx xxxx" />
          <label>收件邮箱（留空＝同发件）</label>
          <input id="emailTo" type="text" placeholder="chentongfrance@gmail.com" />
          <p class="hint">在 Google 账号开启两步验证后，到 <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer">应用专用密码</a> 生成一个 16 位密码填到上面（不是登录密码）。</p>
        </div>
        <div id="pushFields" style="display:none">
          <label>PushPlus token</label>
          <input id="pushToken" type="text" placeholder="在 pushplus.plus 微信扫码登录后复制" />
          <p class="hint">打开 <a href="https://www.pushplus.plus/" target="_blank" rel="noreferrer">pushplus.plus</a> → 微信扫码登录 → 复制 token（需 ¥1 一次性实名）。</p>
        </div>
        <div class="actions">
          <button id="testBtn" class="secondary" type="button">测试提醒</button>
        </div>
        <p id="notifyStatus" class="hint"></p>
      </div>

      <div class="actions">
        <button id="saveBtn">保存配置</button>
        <button id="runBtn" class="secondary">立即扫描</button>
      </div>
    </aside>

    <section class="content">
      <div class="status">
        <span id="scanStatus">未扫描</span>
        <span id="count">0 条</span>
      </div>
      <div class="toolbar">
        <input id="q" placeholder="搜索标题/正文" />
        <select id="stationFilter"><option value="">全部站点</option></select>
        <select id="platformFilter"><option value="">全部来源</option><option value="华人街">华人街</option><option value="新欧洲">新欧洲</option></select>
        <select id="wholeFilter"><option value="1">排除合租</option><option value="0">全部</option></select>
      </div>
      <div id="list" class="list"></div>
    </section>
  </main>
  <script>
    const stationNames = ["Cite Universitaire","Gentilly","Laplace","Arcueil-Cachan","Bagneux","Bourg-la-Reine","Parc de Sceaux"];
    const sourceDefs = [["huarenjie","华人街"],["xineurope","新欧洲"]];
    const $ = (id) => document.getElementById(id);
    let config = null;

    function esc(v) { return String(v ?? "").replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s])); }

    async function api(path, opts) {
      const res = await fetch(path, opts);
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }

    function renderStations(selected) {
      $("stations").innerHTML = stationNames.map(name => `
        <label class="check"><input type="checkbox" value="${esc(name)}" ${selected.includes(name) ? "checked" : ""}> ${esc(name)}</label>
      `).join("");
      $("stationFilter").innerHTML = `<option value="">全部站点</option>` + stationNames.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
    }

    function renderSources(selected) {
      $("sources").innerHTML = sourceDefs.map(([key,label]) => `
        <label class="check"><input type="checkbox" value="${key}" ${selected.includes(key) ? "checked" : ""}> ${esc(label)}</label>
      `).join("");
    }

    async function loadConfig() {
      config = await api("/api/config");
      $("maxPages").value = config.max_pages;
      $("maxRent").value = config.max_rent;
      $("months").value = config.months;
      $("interval").value = config.scan_interval_min;
      $("wholeOnly").checked = config.whole_only;
      $("autoScan").checked = config.auto_scan;
      $("notifyEnabled").checked = config.notify_enabled;
      $("notifyChannel").value = config.notify_channel || "email";
      $("smtpUser").value = config.smtp_user || "";
      $("smtpPass").value = config.smtp_app_password || "";
      $("emailTo").value = config.email_to || "";
      $("pushToken").value = config.pushplus_token || "";
      $("includeExtra").value = config.include_extra || "";
      $("excludeExtra").value = config.exclude_extra || "";
      renderStations(config.selected_stations || []);
      renderSources(config.sources || []);
      toggleChannel();
    }

    function toggleChannel() {
      const ch = $("notifyChannel").value;
      $("emailFields").style.display = ch === "email" ? "" : "none";
      $("pushFields").style.display = ch === "pushplus" ? "" : "none";
    }

    function collectConfig() {
      return {
        max_pages: Number($("maxPages").value || 20),
        max_rent: Number($("maxRent").value || 1000),
        months: Number($("months").value || 3),
        scan_interval_min: Number($("interval").value || 10),
        whole_only: $("wholeOnly").checked,
        auto_scan: $("autoScan").checked,
        notify_enabled: $("notifyEnabled").checked,
        notify_channel: $("notifyChannel").value,
        smtp_user: $("smtpUser").value.trim(),
        smtp_app_password: $("smtpPass").value.trim(),
        email_to: $("emailTo").value.trim(),
        pushplus_token: $("pushToken").value.trim(),
        selected_stations: [...document.querySelectorAll("#stations input:checked")].map(el => el.value),
        sources: [...document.querySelectorAll("#sources input:checked")].map(el => el.value),
        include_extra: $("includeExtra").value,
        exclude_extra: $("excludeExtra").value,
      };
    }

    async function saveConfig() {
      config = await api("/api/config", { method:"POST", body: JSON.stringify(collectConfig()) });
      renderStations(config.selected_stations || []);
      renderSources(config.sources || []);
      return config;
    }

    async function runScan() {
      await saveConfig();
      await api("/api/run", { method:"POST" });
      pollStatus();
    }

    async function testNotify() {
      $("notifyStatus").textContent = "推送中…";
      try {
        await saveConfig();
        const r = await api("/api/test_notify", { method:"POST" });
        $("notifyStatus").textContent = (r.ok ? "✅ " : "❌ ") + (r.message || (r.ok ? "已发送" : "失败"));
      } catch (e) { $("notifyStatus").textContent = "❌ " + e.message; }
    }

    async function pollStatus() {
      const st = await api("/api/status");
      $("runBtn").disabled = st.running;
      $("topStatus").textContent = st.running ? "扫描中…" : (config && config.auto_scan ? "自动监控中" : "准备就绪");
      $("scanStatus").textContent = `${st.message} · 新增 ${st.inserted} · 命中 ${st.matched}` + (st.last_notify ? ` · ${st.last_notify}` : "");
      if (st.running) setTimeout(pollStatus, 1500);
      await loadListings();
    }

    async function loadListings() {
      const qs = new URLSearchParams({ q: $("q").value, station: $("stationFilter").value, platform: $("platformFilter").value, whole_only: $("wholeFilter").value });
      const rows = await api("/api/listings?" + qs.toString());
      $("count").textContent = `${rows.length} 条`;
      $("list").innerHTML = rows.length ? rows.map(row => {
        const pcls = row.platform === "新欧洲" ? "xe" : "hrj";
        return `
        <article class="item">
          <div>
            <h2><a href="${esc(row.url)}" target="_blank" rel="noreferrer">${esc(row.title)}</a></h2>
            <div class="meta">
              <span class="badge ${pcls}">${esc(row.platform || "华人街")}</span>
              <span class="badge">${esc(row.updated || "未知日期")}</span>
              <span class="badge">${esc(row.matched_stations || "未标站点")}</span>
              <span>${esc(row.area || "")}</span>
              <span>${esc(row.source || "")}</span>
              <span>${row.is_share ? "合租/分租" : "整租命中"}</span>
            </div>
            <p class="excerpt">${esc(row.excerpt)}</p>
          </div>
          <div class="price">${esc(row.rent || "价格待确认")}</div>
        </article>`;
      }).join("") : `<div class="empty">暂无结果。保存配置后点「立即扫描」。</div>`;
    }

    $("saveBtn").addEventListener("click", saveConfig);
    $("runBtn").addEventListener("click", runScan);
    $("testBtn").addEventListener("click", testNotify);
    $("notifyChannel").addEventListener("change", toggleChannel);
    $("q").addEventListener("input", loadListings);
    $("stationFilter").addEventListener("change", loadListings);
    $("platformFilter").addEventListener("change", loadListings);
    $("wholeFilter").addEventListener("change", loadListings);

    loadConfig().then(pollStatus);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload: object, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            raw = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if parsed.path == "/api/config":
            self.send_json(get_config())
            return
        if parsed.path == "/api/status":
            with state_lock:
                payload = dict(scan_state)
            self.send_json(payload)
            return
        if parsed.path == "/api/listings":
            self.send_json(listings(parse_qs(parsed.query)))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json({"error": "invalid json"}, 400)
            return
        if parsed.path == "/api/config":
            self.send_json(set_config(payload))
            return
        if parsed.path == "/api/run":
            with state_lock:
                if scan_state["running"]:
                    self.send_json({"ok": False, "error": "scan already running"}, 409)
                    return
                scan_state["running"] = True
            run_scan_async()
            self.send_json({"ok": True})
            return
        if parsed.path == "/api/test_notify":
            cfg = get_config()
            sample = (
                "<p>这是一条来自「华人街 + 新欧洲 租房监控」的测试消息。</p>"
                "<p>命中新房源时你会收到类似格式的提醒。</p>"
            )
            ok, msg = send_notification("🔔 租房监控测试提醒", sample, cfg)
            self.send_json({"ok": ok, "message": msg})
            return
        self.send_error(404)

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
