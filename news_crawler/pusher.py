"""推送通知：Bark（iOS 锁屏）与 Server酱（微信服务号）。

PUSH_CHANNELS 配置启用通道；单通道失败只记日志，不影响其余通道与主流程。
"""
from __future__ import annotations

import logging
import urllib.parse

import requests

log = logging.getLogger(__name__)

_TIMEOUT = 10
_KNOWN_CHANNELS = ("bark", "serverchan")


def push_bark(bark_url: str, title: str, body: str, level: str = "active") -> None:
    """bark_url 形如 https://api.day.app/<deviceKey>（App Store 装 Bark 即得）。"""
    base = bark_url.rstrip("/")
    r = requests.get(
        f"{base}/{urllib.parse.quote(title, safe='')}/{urllib.parse.quote(body, safe='')}",
        params={"level": level, "sound": "birdsong"}, timeout=_TIMEOUT)
    r.raise_for_status()


def push_serverchan(send_key: str, title: str, desp_markdown: str) -> None:
    r = requests.post(f"https://sctapi.ftqq.com/{send_key}.send",
                      data={"title": title, "desp": desp_markdown}, timeout=_TIMEOUT)
    r.raise_for_status()


def notify(channels, bark_url: str | None, send_key: str | None,
           title: str, body: str, desp_md: str | None = None) -> int:
    """向启用通道推送；返回成功通道数。任何失败仅记录日志，绝不抛出。"""
    md = desp_md or body
    ok = 0
    for ch in channels:
        try:
            if ch == "bark":
                if not bark_url:
                    raise ValueError("BARK_URL 未配置")
                push_bark(bark_url, title, body)
            elif ch == "serverchan":
                if not send_key:
                    raise ValueError("SERVERCHAN_SEND_KEY 未配置")
                push_serverchan(send_key, title, md)
            else:
                raise ValueError(f"未知通道 {ch!r}")
            ok += 1
            log.info("推送成功：%s", ch)
        except Exception as exc:
            log.warning("推送失败（%s）：%s", ch, exc)
    return ok
