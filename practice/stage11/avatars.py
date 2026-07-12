"""
每個 agent（persona／facilitator／master／baseline／system）配一張頭像，
純本地決定性產生（identicon 風格），不打任何圖像生成 API：同一個 id 每次都
拿到同一張圖，零成本、即時。

跟 static/index.html 裡的 avatarHtml()／hashCode() 是同一個演算法的兩份
獨立實作（Python 版給未來要把頭像嵌進靜態回放頁的場景用，JS 版給即時畫面
用，即時畫面不想為了一張頭像多打一次 API）——兩邊只要「同一個 id 每次都拿到
一致的顏色」這個不變量互相對齊即可，不需要逐 pixel 一致。
"""
from __future__ import annotations

import hashlib
import html


def _hue(seed: str) -> int:
    """跟 static/index.html 的 hashCode() 邏輯對齊：字串雜湊後對 360 取餘數
    當色相。用 md5 而不是 Python 內建 hash()，因為內建 hash() 對字串加了
    隨機種子（PYTHONHASHSEED），同一個 id 在不同 process 會拿到不同結果，
    頭像顏色就不 deterministic 了。"""
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 360


def _initials(name: str) -> str:
    return (name or "?").strip()[:1] or "?"


def svg_avatar(persona_id: str, name: str, *, size: int = 40) -> str:
    """回傳一段可以直接內嵌進 HTML 的 <svg> 字串：實心圓底色 + 姓名首字。"""
    seed = persona_id or name or "?"
    hue = _hue(seed)
    # personas.yaml 是使用者自己編輯的本機檔案，不是不受信任的外部輸入，但
    # initials 只取姓名第一個字元，理論上還是可能剛好是 < & " 這種會弄壞
    # SVG/HTML 結構的字元（例如某個 persona 名字取作「<測試>」）——escape
    # 一下不吃虧，跟 stage3 起 render_landing_page_html() 一路沿用的
    # html.escape() 保護原則一致。
    initials = html.escape(_initials(name))
    half = size / 2
    font_size = size * 0.42
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" role="img" aria-label="{initials} 的頭像">'
        f'<circle cx="{half}" cy="{half}" r="{half}" fill="hsl({hue},55%,42%)" />'
        f'<text x="{half}" y="{half}" text-anchor="middle" dominant-baseline="central" '
        f'font-family="-apple-system,\'PingFang TC\',sans-serif" font-weight="700" '
        f'font-size="{font_size}" fill="white">{initials}</text>'
        f'</svg>'
    )
