"""Startup page（入口加载页）：沿用桌面与产品共用的图标、颜色和布局。"""
from __future__ import annotations

import json
from pathlib import Path


def render_startup_page(root: Path, document: str) -> str:
    """render_startup_page（渲染入口页）：替换统一样式与图标占位符。"""
    assets = root / "apps/desktop/boot"  # assets（统一加载画面的源资源目录）。
    if not (assets / "startup.css").is_file():
        assets = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file()) / "apps/desktop/boot"
    style = (assets / "startup.css").read_text(encoding="utf-8")
    icon = (assets / "app-icon.svg").read_text(encoding="utf-8")
    return document.replace("__STARTUP_STYLE__", style).replace("__STARTUP_ICON__", icon)


def loading_page(root: Path, destination: str = "/") -> str:
    # 一次性入口建立会话后继续同源跳转；画面保持一致，令牌不进入目标地址。
    document = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\"><title>Voice Terminal · 语音终端</title>"
            "<style>html,body{margin:0;background:#0e1118}__STARTUP_STYLE__</style></head>"
            "<body><main class=\"startup-screen\" role=\"status\"><section class=\"startup-content\">"
            "<div class=\"startup-icon\" role=\"img\" aria-label=\"语音终端\">__STARTUP_ICON__</div>"
            "<h1 class=\"startup-title\">应用启动中</h1><p class=\"startup-status\">正在加载应用界面</p>"
            "<div class=\"startup-progress\" data-indeterminate=\"true\" role=\"progressbar\" aria-label=\"应用启动进度\">"
            "<span class=\"startup-progress-fill\"></span></div><div class=\"startup-actions\">"
            "<button class=\"startup-button\" type=\"button\" onclick=\"window.__TAURI_INTERNALS__?.invoke('skip_startup')\">跳过</button>"
            "</div></section></main><script>location.replace(" + json.dumps(destination) + ")</script></body></html>")
    return render_startup_page(root, document)
