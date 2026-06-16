#!/usr/bin/env python3
"""云端定时任务版（GitHub Actions 用）。

与本地 app.py 不同：不用 SQLite、不起 Web 服务，状态存在 state.json 里。
复用 app.py 里的过滤逻辑和两个爬虫模块。机密(发件邮箱/密码/收件人)只从
环境变量读取（GitHub Secrets 注入），绝不写进仓库。

state.json 结构：{"seen": [房源id...], "seeded": bool, "last_run": "..."}
- 首次运行(seeded=false)只建立基线、不发邮件，避免把历史房源一次性轰炸。
- 之后只对「之前没见过」的命中房源发邮件。
"""
import datetime as dt
import json
import os
from pathlib import Path

import app
import huarenjie_rent_scraper as hrj
import notifier

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
SEEN_CAP = 5000  # 限制 state 体积，只保留最近的 id


def load_json(path: Path, default: dict) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def main() -> int:
    cfg = load_json(CONFIG_PATH, {})
    # 用默认值补齐缺失项，并叠加机密（来自环境变量）
    merged = {**app.DEFAULT_CONFIG, **cfg}
    merged["notify_channel"] = "email"
    merged["notify_enabled"] = True
    merged["smtp_user"] = os.environ.get("SMTP_USER", "")
    merged["smtp_app_password"] = os.environ.get("SMTP_APP_PASSWORD", "")
    merged["email_to"] = os.environ.get("EMAIL_TO", os.environ.get("SMTP_USER", ""))

    state = load_json(STATE_PATH, {"seen": [], "seeded": False})
    seen_ids = set(state.get("seen", []))
    seeded = bool(state.get("seeded", False))

    cutoff = dt.date.today() - dt.timedelta(days=int(merged["months"]) * 31)
    run_seen: set[str] = set()
    matched_ids: set[str] = set()
    new_listings: list[hrj.Listing] = []
    errors: list[str] = []

    def process(listing: hrj.Listing) -> None:
        if listing.id in run_seen:
            return
        run_seen.add(listing.id)
        if not hrj.within_cutoff(listing.updated, cutoff):
            return
        if not app.relevant_detail(listing, merged):
            return
        matched_ids.add(listing.id)
        if listing.id not in seen_ids:
            new_listings.append(listing)

    if "huarenjie" in merged["sources"]:
        app.scan_huarenjie(merged, process, errors)
    if "xineurope" in merged["sources"]:
        app.scan_xineurope(merged, process, errors)

    sent_msg = "（首次基线，不发邮件）" if not seeded else "（无新房源）"
    if new_listings and seeded:
        title = f"🏠 {len(new_listings)} 条新房源 · Arcueil/Cachan/Laplace ≤{merged['max_rent']}€"
        ok, sent_msg = notifier.send_email(
            merged["smtp_user"],
            merged["smtp_app_password"],
            merged["email_to"],
            title,
            notifier.email_body(title, notifier.build_html(new_listings)),
        )
        sent_msg = ("已发送：" if ok else "发送失败：") + sent_msg

    # 更新状态：把本轮所有命中 id 记为已见（含 new），限制体积
    updated_seen = list(seen_ids | matched_ids)[-SEEN_CAP:]
    STATE_PATH.write_text(
        json.dumps(
            {
                "seen": updated_seen,
                "seeded": True,
                "last_run": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"命中 {len(matched_ids)} | 新房源 {len(new_listings)} | {sent_msg} | "
        f"错误 {len(errors)}",
        flush=True,
    )
    for err in errors[-5:]:
        print("  ERROR:", err, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
