"""Render an .excalidraw file to PNG (no browser required)."""
import json
import math
import sys
from PIL import Image, ImageDraw, ImageFont

SRC = r"C:\Workshops\LakebaseMigrationDemo\docs\architecture.excalidraw"
OUT = r"C:\Workshops\LakebaseMigrationDemo\docs\architecture.png"
SCALE = 2
PAD = 40

doc = json.load(open(SRC, encoding="utf-8"))
els = [e for e in doc["elements"] if not e.get("isDeleted")]

xs = [e["x"] for e in els] + [e["x"] + e.get("width", 0) for e in els]
ys = [e["y"] for e in els] + [e["y"] + e.get("height", 0) for e in els]
minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
W = int((maxx - minx + 2 * PAD) * SCALE)
H = int((maxy - miny + 2 * PAD) * SCALE)

img = Image.new("RGB", (W, H), "#ffffff")
d = ImageDraw.Draw(img)

FONTS = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\DejaVuSans.ttf",
]
BOLDS = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]
_cache = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _cache:
        for p in (BOLDS if bold else FONTS):
            try:
                _cache[key] = ImageFont.truetype(p, size)
                break
            except OSError:
                continue
        else:
            _cache[key] = ImageFont.load_default()
    return _cache[key]


def T(x, y):
    return ((x - minx + PAD) * SCALE, (y - miny + PAD) * SCALE)


def lighten(hexcol, amt=0.55):
    """Blend toward white so hachure fills read as soft tints."""
    hexcol = hexcol.lstrip("#")
    r, g, b = (int(hexcol[i:i + 2], 16) for i in (0, 2, 4))
    return tuple(int(c + (255 - c) * amt) for c in (r, g, b))


def dashed_line(p0, p1, col, w, dash=14 * SCALE, gap=10 * SCALE):
    x0, y0 = p0
    x1, y1 = p1
    total = math.hypot(x1 - x0, y1 - y0)
    if total == 0:
        return
    ux, uy = (x1 - x0) / total, (y1 - y0) / total
    pos = 0.0
    while pos < total:
        seg = min(dash, total - pos)
        d.line([(x0 + ux * pos, y0 + uy * pos),
                (x0 + ux * (pos + seg), y0 + uy * (pos + seg))], fill=col, width=w)
        pos += dash + gap


def wrap(txt, fnt, maxw):
    out = []
    for para in txt.split("\n"):
        if not para:
            out.append("")
            continue
        words, line = para.split(" "), ""
        for wd in words:
            trial = (line + " " + wd).strip()
            if d.textlength(trial, font=fnt) <= maxw or not line:
                line = trial
            else:
                out.append(line)
                line = wd
        out.append(line)
    return out


# ---- pass 1: rectangles -------------------------------------------------
for e in els:
    if e["type"] != "rectangle":
        continue
    p0, p1 = T(e["x"], e["y"]), T(e["x"] + e["width"], e["y"] + e["height"])
    sw = max(1, int(e.get("strokeWidth", 1) * SCALE))
    bg = e.get("backgroundColor", "transparent")
    fill = lighten(bg, 0.45) if bg and bg != "transparent" else None
    r = 14 * SCALE
    if e.get("strokeStyle") == "dashed":
        d.rounded_rectangle([p0, p1], radius=r, fill=fill, outline=None)
        # emulate a dashed border
        for a, b in ((( p0[0], p0[1]), (p1[0], p0[1])),
                     ((p1[0], p0[1]), (p1[0], p1[1])),
                     ((p1[0], p1[1]), (p0[0], p1[1])),
                     ((p0[0], p1[1]), (p0[0], p0[1]))):
            dashed_line(a, b, e["strokeColor"], sw)
    else:
        d.rounded_rectangle([p0, p1], radius=r, fill=fill,
                            outline=e["strokeColor"], width=sw)

# ---- pass 2: arrows -----------------------------------------------------
for e in els:
    if e["type"] != "arrow":
        continue
    pts = [T(e["x"] + px, e["y"] + py) for px, py in e["points"]]
    sw = max(1, int(e.get("strokeWidth", 2) * SCALE))
    col = e["strokeColor"]
    for i in range(len(pts) - 1):
        if e.get("strokeStyle") == "dashed":
            dashed_line(pts[i], pts[i + 1], col, sw)
        else:
            d.line([pts[i], pts[i + 1]], fill=col, width=sw)
    # arrowhead
    (ax, ay), (bx, by) = pts[-2], pts[-1]
    ang = math.atan2(by - ay, bx - ax)
    L = 16 * SCALE
    d.polygon([(bx, by),
               (bx - L * math.cos(ang - 0.42), by - L * math.sin(ang - 0.42)),
               (bx - L * math.cos(ang + 0.42), by - L * math.sin(ang + 0.42))],
              fill=col)

# ---- pass 3: text -------------------------------------------------------
by_id = {e["id"]: e for e in els}
for e in els:
    if e["type"] != "text":
        continue
    size = int(e.get("fontSize", 14) * SCALE)
    bold = size >= 17 * SCALE or e.get("containerId") is None
    fnt = font(size, bold=bold)
    maxw = e["width"] * SCALE
    lines = wrap(e["text"], fnt, maxw)
    lh = size * 1.34
    x0, y0 = T(e["x"], e["y"])
    cid = e.get("containerId")
    if cid and cid in by_id:
        c = by_id[cid]
        cx0, cy0 = T(c["x"], c["y"])
        cx1, cy1 = T(c["x"] + c["width"], c["y"] + c["height"])
        y0 = (cy0 + cy1) / 2 - (len(lines) * lh) / 2
        cx = (cx0 + cx1) / 2
        for i, ln in enumerate(lines):
            d.text((cx - d.textlength(ln, font=fnt) / 2, y0 + i * lh), ln,
                   font=fnt, fill=e.get("strokeColor", "#000000"))
    else:
        align = e.get("textAlign", "left")
        for i, ln in enumerate(lines):
            tx = x0
            if align == "center":
                tx = x0 + (maxw - d.textlength(ln, font=fnt)) / 2
            d.text((tx, y0 + i * lh), ln, font=fnt,
                   fill=e.get("strokeColor", "#000000"))

img.save(OUT)
print(f"wrote {OUT}  {img.width}x{img.height}")
