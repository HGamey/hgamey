#!/usr/bin/env python3
"""实时看板文字对比度检查：从 dash_static/app.css 读浅色 / 深色两套颜色令牌，
把页面上实际出现的「文字色 × 背景色」组合逐一按 WCAG 2 算对比度。
门槛：所有文字 ≥ 4.5:1；正文（ink）最好 ≥ 7:1。任何一对不到 4.5 就以退出码 1 结束。
用法：python3 node/tools/contrast_check.py [--md]   （--md 输出 Markdown 表，贴进 PR）"""
import os, re, sys

CSS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dash_static", "app.css")

# (文字令牌, 背景令牌, 页面上哪里用到)
PAIRS = [
    ("ink", "card", "正文、标题、大号数字"), ("ink", "page", "页面底上的「现在」一行、区块标题"),
    ("ink", "panel", "数字卡片"), ("ink", "wash", "代码片段、手机卡上的状态条"),
    ("ink", "brown-soft", "时间线里「读到你的新指令」"), ("ink", "blue-soft", "导航当前页"),
    ("ink", "amber-soft", "掌舵解锁提示条"),
    ("ink2", "card", "次要说明"), ("ink2", "page", "页面底上的次要说明"), ("ink2", "panel", "数字卡片里的说明"),
    ("ink2", "wash", "手机卡状态条"), ("ink2", "brown-soft", "指令的理解"), ("ink2", "amber-soft", "提示条"),
    ("muted", "card", "时间戳"), ("muted", "page", "时间戳（页面底）"), ("muted", "panel", "数字卡片标签"),
    ("blue-ink", "card", "进行中、链接、图表目标线文字"), ("blue-ink", "blue-soft", "「进行中」状态牌"),
    ("amber-ink", "card", "「淘汰」文字"), ("amber-ink", "amber-soft", "「等你确认 / 暂停」状态牌"),
    ("teal-ink", "card", "「正在测」文字"), ("teal-ink", "teal-soft", "「正在测」标记"),
    ("good-ink", "card", "「保留 / 变好」文字"), ("bad-ink", "card", "「变差 / 停止」文字"), ("bad-ink", "bad-soft", "「失败」状态牌"),
    ("brown", "card", "示例数据标签"), ("brown", "brown-soft", "示例数据标签"), ("green", "teal-soft", "「已完成」状态牌"),
    ("on-blue", "blue", "主按钮文字"),
]


def tokens(block):
    return {k: v.upper() for k, v in re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", block)}


def lum(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def main():
    css = open(CSS, encoding="utf-8").read()
    light = tokens(re.search(r":root \{(.*?)\n\}", css, re.S).group(1))
    dark = {**light, **tokens(re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', css, re.S).group(1))}
    md = "--md" in sys.argv
    bad = 0
    if md:
        print("| 文字 | 背景 | 用在哪 | 浅色 | 深色 |\n|---|---|---|---|---|")
    for fg, bg, where in PAIRS:
        cells = []
        for name, t in (("浅色", light), ("深色", dark)):
            r = ratio(t[fg], t[bg])
            need = 7.0 if fg == "ink" else 4.5
            ok = "✓" if r >= need else ("△" if r >= 4.5 else "✗")
            bad += r < 4.5
            cells.append(f"{t[fg]} / {t[bg]} = {r:.2f} {ok}")
        print(f"| `{fg}` | `{bg}` | {where} | {cells[0]} | {cells[1]} |" if md else f"{fg:10} on {bg:11} {cells[0]:40} {cells[1]:40} {where}")
    print(("\n" if md else "") + f"不到 4.5:1 的组合：{bad} 个（✓ 达标；正文 ink 的门槛是 7:1，△ = 4.5–7）")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
