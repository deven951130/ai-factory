"""產生 app.ico（工廠剪影）。只在要改圖示時執行：需要 Pillow。

    python packaging/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # 先畫大圖再縮小，邊緣比較平滑
top, bottom = (59, 130, 246), (29, 78, 216)  # 由上往下漸層
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    gd.line((0, y, S, y), fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((32, 32, S - 32, S - 32), radius=200, fill=255)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(grad, mask=mask)
d = ImageDraw.Draw(img)

white, base = "#ffffff", 800
d.rectangle((640, 210, 730, 520), fill=white)  # 煙囪
roof = [(200, base), (200, 420), (360, 540), (360, 420), (520, 540), (520, 420), (680, 540), (680, 420), (824, 520), (824, base)]
d.polygon(roof, fill=white)  # 鋸齒屋頂的廠房
for x in (270, 430, 590):  # 窗戶
    d.rounded_rectangle((x, 620, x + 90, 700), radius=14, fill="#1d4ed8")
d.ellipse((700, 120, 790, 210), fill="#f59e0b")  # 煙 / 火花
d.ellipse((780, 60, 840, 120), fill="#f59e0b")

out = Path(__file__).resolve().parent / "app.ico"
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out)
