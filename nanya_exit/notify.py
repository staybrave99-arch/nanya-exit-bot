"""ntfy 推播。

用 POST + header 帶標題/標籤，訊息本文放在 body ——
不要用 GET 把內容塞在 query string，那樣中文要 encode、長度也有限。
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class NotifyError(RuntimeError):
    pass


def push(server: str, topic: str, title: str, message: str,
         tags: str = "", priority: int = 3, token: str = "",
         timeout: int = 30, dry_run: bool = False) -> dict | None:
    """送一則 ntfy 通知。dry_run=True 只印不送。"""
    url = f"{server.rstrip('/')}/{topic}"
    headers = {"Priority": str(priority)}

    # HTTP header 只吃 latin-1，中文標題必須用 RFC 2047 base64 編碼，
    # 否則 requests 會丟 UnicodeEncodeError。ntfy 看得懂這個格式。
    if title.isascii():
        headers["Title"] = title
    else:
        import base64
        b = base64.b64encode(title.encode("utf-8")).decode("ascii")
        headers["Title"] = f"=?UTF-8?B?{b}?="
    if tags:
        headers["Tags"] = tags
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if dry_run:
        log.info("[dry-run] ntfy → %s\nTitle: %s\n%s", url, title, message)
        return None

    try:
        r = requests.post(url, data=message.encode("utf-8"),
                          headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:                     # noqa: BLE001
        raise NotifyError(f"ntfy 推播失敗（{url}）：{e}") from e
