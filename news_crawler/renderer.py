"""HTML 渲染（FR-5）：jinja2 模板 → 邮件正文。

autoescape 必须显式为 True：模板文件名以 .j2 结尾，select_autoescape 按扩展名
判断不会生效（单测 test_render_escapes_html_in_content 防回归）。
"""
from __future__ import annotations

import os

from jinja2 import Environment, FileSystemLoader

from .config import APP_DIR
from .digest import Digest

TEMPLATE_DIR = os.path.join(APP_DIR, "templates")


def render_digest_html(digest: Digest) -> str:
    environment = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    template = environment.get_template("digest.html.j2")
    return template.render(
        day=digest.day,
        generated_at=digest.generated_at,
        sections=digest.sections,
        stats=digest.stats,
    )
