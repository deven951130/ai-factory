"""從 Oneiroverse Logo 原圖（packaging/brand/oneiroverse-dark.jpg）拆出啟動動畫的透明圖層。

輸出 frontend/oneiroverse/{crescent,o,wordmark}.png，三張同尺寸、疊起來就是原本的 Logo（不含星空背景）。
需要 Pillow：.venv-build\\Scripts\\python.exe packaging\\make_splash.py [預覽輸出資料夾]
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

HERE = Path(__file__).resolve().parent
SRC = HERE / "brand" / "oneiroverse-dark.jpg"
OUT = HERE.parent / "frontend" / "oneiroverse"
NOISE = 8      # 減掉背景後低於此值視為星空雜訊
CORE = 70      # 亮度高於此值算筆畫本體（用來分組）
DOWN = 4       # 分組與擴張遮罩時的縮小倍率
MARGIN = 28    # 輸出畫布在筆畫外保留的光暈範圍
BASE = (6, 22, 43)  # 原圖 Logo 周圍的背景色 = 啟動畫面底色 #06162b（splash.js 要一致）


def components(mask: Image.Image, min_size: int) -> list[set[int]]:
    w, h = mask.size
    px = mask.tobytes()
    seen = bytearray(len(px))
    found = []
    for start, v in enumerate(px):
        if not v or seen[start]:
            continue
        seen[start] = 1
        comp, q = {start}, deque([start])
        while q:
            i = q.popleft()
            x, y = i % w, i // w
            for j in (i - 1 if x else -1, i + 1 if x < w - 1 else -1, i - w if y else -1, i + w if y < h - 1 else -1):
                if j >= 0 and px[j] and not seen[j]:
                    seen[j] = 1
                    comp.add(j)
                    q.append(j)
        if len(comp) >= min_size:
            found.append(comp)
    return sorted(found, key=len, reverse=True)


def paint(size: tuple[int, int], pixels: set[int]) -> Image.Image:
    buf = bytearray(size[0] * size[1])
    for i in pixels:
        buf[i] = 255
    return Image.frombytes("L", size, bytes(buf))


def halo(core_small: Image.Image, size: tuple[int, int]) -> Image.Image:
    """筆畫本體往外擴張成帶羽化邊緣的遮罩，涵蓋光暈。"""
    grown = core_small.filter(ImageFilter.MaxFilter(7)).resize(size, Image.BILINEAR)
    return grown.filter(ImageFilter.GaussianBlur(8)).point(lambda v: min(255, v * 2))


def value(im: Image.Image) -> Image.Image:
    # 用 RGB 最大值而不是亮度：月牙上端是暗紫色，亮度很低但仍是筆畫。
    r, g, b = im.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def main(preview: Path | None) -> None:
    img = Image.open(SRC).convert("RGB")
    w, h = img.size
    # 背景：縮小後取區域最小值（去掉 Logo 與星點）再模糊放大。
    small = img.resize((w // 8, h // 8), Image.BOX).filter(ImageFilter.MinFilter(9)).filter(ImageFilter.GaussianBlur(3))
    bg = small.resize((w, h), Image.BILINEAR)
    light = ImageChops.subtract(img, bg).point(lambda v: 0 if v <= NOISE else round((v - NOISE) * 255 / (255 - NOISE)))

    sw, sh = w // DOWN, h // DOWN
    core = value(light).resize((sw, sh), Image.BOX).point(lambda v: 255 if v > CORE else 0)
    comps = components(core, min_size=6)
    rows = sorted({i // sw for c in comps for i in c})
    gap = max(zip(rows, rows[1:]), key=lambda p: p[1] - p[0])  # Logo 圖形與字標之間最大的空白列
    emblem = [c for c in comps if max(i // sw for i in c) <= gap[0]]
    text = set().union(*(c for c in comps if min(i // sw for i in c) >= gap[1]))
    crescent = emblem[0]  # 最大塊
    o = set().union(*emblem[1:])
    groups = {"crescent": crescent, "o": o, "wordmark": text}
    for name, g in groups.items():
        xs, ys = [i % sw * DOWN for i in g], [i // sw * DOWN for i in g]
        print(f"{name:9s} bbox x {min(xs)}-{max(xs)}  y {min(ys)}-{max(ys)}  ({len(g)} px @1/{DOWN})")

    masks = {n: halo(paint((sw, sh), g), (w, h)) for n, g in groups.items()}
    # 每個像素只歸給遮罩值最大的圖層：三層全部顯示時剛好等於原圖，光暈不會重複疊加。
    top = ImageChops.lighter(ImageChops.lighter(masks["crescent"], masks["o"]), masks["wordmark"])
    taken = Image.new("L", (w, h), 0)
    layers = {}
    for n, m in masks.items():
        mine = ImageChops.subtract(ImageChops.subtract(m, top).point(lambda v: 255 if v == 0 else 0), taken)
        mine = ImageChops.multiply(mine, m.point(lambda v: 255 if v else 0))
        taken = ImageChops.lighter(taken, mine)
        weight = ImageChops.multiply(m, mine)
        layers[n] = ImageChops.multiply(light, Image.merge("RGB", (weight,) * 3))

    union = value(ImageChops.lighter(ImageChops.lighter(layers["crescent"], layers["o"]), layers["wordmark"]))
    x0, y0, x1, y1 = union.point(lambda v: 255 if v > CORE else 0).getbbox()
    box = (max(0, x0 - MARGIN), max(0, y0 - MARGIN), min(w, x1 + MARGIN), min(h, y1 + MARGIN))
    print("canvas", box, f"{box[2] - box[0]}x{box[3] - box[1]}")

    OUT.mkdir(parents=True, exist_ok=True)
    for n, layer in layers.items():
        crop = layer.crop(box)
        # Logo 是疊在背景上的光（原圖 = 背景 + 光）。以啟動畫面底色 BASE 做 color-to-alpha：
        # alpha = max(光 / (255 - 底色))，預乘色 = 光 + 底色 × alpha；一般疊圖在 BASE 上就等於原圖。
        chans = crop.split()
        alpha = ImageChops.lighter(ImageChops.lighter(
            *(c.point(lambda v, b=b: min(255, round(v * 255 / (255 - b)))) for c, b in zip(chans[:2], BASE[:2]))),
            chans[2].point(lambda v: min(255, round(v * 255 / (255 - BASE[2])))))
        pre = [ImageChops.add(c, alpha.point(lambda v, b=b: round(v * b / 255))) for c, b in zip(chans, BASE)]
        Image.merge("RGBa", (*pre, alpha)).convert("RGBA").save(OUT / f"{n}.png", optimize=True)
        print("wrote", OUT / f"{n}.png")

    # 動畫需要的幾何資訊（相對畫布）：月牙中心、兩端尖角的角度（CSS conic-gradient：0deg 朝上、順時針）。
    import math

    cx_pts = [(i % sw * DOWN + DOWN / 2 - box[0], i // sw * DOWN + DOWN / 2 - box[1]) for i in crescent]
    cxs, cys = [p[0] for p in cx_pts], [p[1] for p in cx_pts]
    center = ((min(cxs) + max(cxs)) / 2, (min(cys) + max(cys)) / 2)
    tip_top = min(cx_pts, key=lambda p: p[1])
    tip_end = max(cx_pts, key=lambda p: p[0])

    def css_angle(p):
        return math.degrees(math.atan2(p[0] - center[0], -(p[1] - center[1]))) % 360

    bw, bh = box[2] - box[0], box[3] - box[1]
    print(f"crescent center {center[0] / bw * 100:.1f}% {center[1] / bh * 100:.1f}%  "
          f"tip_top {css_angle(tip_top):.0f}deg  tip_end {css_angle(tip_end):.0f}deg")

    if preview:
        preview.mkdir(parents=True, exist_ok=True)
        base = img.crop(box)
        navy = Image.new("RGB", base.size, BASE)
        for n in layers:
            navy.paste(Image.open(OUT / f"{n}.png"), (0, 0), Image.open(OUT / f"{n}.png"))
        navy.save(preview / "recomposed.png")
        base.save(preview / "original.png")
        print("bg at center", bg.getpixel((w // 2, h // 2)), "corner", bg.getpixel((20, 20)), bg.getpixel((w - 20, h - 20)))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
