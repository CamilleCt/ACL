#!/usr/bin/env python3
"""消息推送：当前实现 PushPlus（微信）。

PushPlus 接口： POST https://www.pushplus.plus/send
body: {"token": "...", "title": "...", "content": "...", "template": "html"}
成功时返回 JSON 的 code == 200。
"""
import html as _html
import json
import smtplib
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

PUSHPLUS_URL = "https://www.pushplus.plus/send"


def _esc(value) -> str:
    return _html.escape(str(value if value is not None else ""))


def build_html(listings: list) -> str:
    """把命中的房源拼成一段 HTML，listings 为 Listing 列表。"""
    blocks = []
    for item in listings:
        rent = _esc(item.rent) or "价格待确认"
        area = _esc(item.area)
        meta = " · ".join(
            part
            for part in [
                _esc(item.platform),
                _esc(item.matched_terms) or _esc(item.region),
                _esc(item.updated) or "日期未知",
            ]
            if part
        )
        excerpt = _esc((item.excerpt or "")[:140])
        blocks.append(
            f'<div style="margin:0 0 14px;padding:10px 12px;border:1px solid #e0e0e0;border-radius:8px">'
            f'<div style="font-size:15px;font-weight:600;line-height:1.4">'
            f'<a href="{_esc(item.url)}" style="color:#0b57d0;text-decoration:none">{_esc(item.title)}</a>'
            f"</div>"
            f'<div style="margin:6px 0;color:#137333;font-weight:700">{rent}'
            f'{("  ·  " + area) if area else ""}</div>'
            f'<div style="color:#5f6368;font-size:13px">{meta}</div>'
            f'<div style="margin-top:6px;color:#3c4043;font-size:13px;line-height:1.5">{excerpt}</div>'
            f"</div>"
        )
    return "".join(blocks) or "<p>（无内容）</p>"


def email_body(title: str, listings_html: str) -> str:
    return (
        '<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:680px;margin:0 auto">'
        f'<h2 style="font-size:18px;color:#202124">{_esc(title)}</h2>'
        f"{listings_html}"
        '<p style="color:#9aa0a6;font-size:12px;margin-top:16px">来自本地「华人街 + 新欧洲 租房监控」</p>'
        "</div>"
    )


def send_email(
    smtp_user: str,
    app_password: str,
    to_addr: str,
    subject: str,
    html_content: str,
    timeout: int = 20,
) -> tuple[bool, str]:
    """通过 Gmail SMTP(SSL 465) 发信。app_password 为 Google 应用专用密码（16 位）。"""
    smtp_user = (smtp_user or "").strip()
    app_password = (app_password or "").replace(" ", "").strip()
    to_addr = (to_addr or "").strip() or smtp_user
    if not smtp_user or not app_password:
        return False, "未配置发件 Gmail 或应用专用密码"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("租房监控", smtp_user))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=timeout) as server:
            server.login(smtp_user, app_password)
            server.sendmail(smtp_user, [to_addr], msg.as_string())
    except Exception as exc:  # noqa: BLE001
        return False, f"发送失败: {exc}"
    return True, f"已发送到 {to_addr}"


def push_pushplus(token: str, title: str, content: str, timeout: int = 15) -> tuple[bool, str]:
    """返回 (是否成功, 说明)。token 为空直接返回失败。"""
    token = (token or "").strip()
    if not token:
        return False, "未配置 PushPlus token"
    payload = json.dumps(
        {"token": token, "title": title, "content": content, "template": "html"}
    ).encode("utf-8")
    req = urllib.request.Request(
        PUSHPLUS_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - 网络异常统一兜底
        return False, f"请求失败: {exc}"
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return False, f"响应非 JSON: {body[:200]}"
    ok = obj.get("code") == 200
    return ok, obj.get("msg", body)
