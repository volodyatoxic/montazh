"""Моушн и саунд-дизайн для вертикального тизера и рилсов: оверлей с альфой, SFX, финальная карточка.

  python3 motion_assets.py <проект> --spec <spec.json> --out <папка>

Читает описание сцены и выдаёт три файла с ASCII-именами:
  overlay.mov   ProRes 4444 с альфой, поверх картинки (титры, типографика)
  sfx.wav       отдельная дорожка: вуши на склейках, удары под титры, райзер к финалу
  endcard.mov   непрозрачная концовка

Кадры рисуются в PIL и отдаются ffmpeg потоком, PNG на диск не пишутся.
Титры появляются по словам: вход 0,22 с easeOutCubic со сдвигом снизу, уход 0,18 с.
Звук исходников не трогается — SFX кладётся своей дорожкой.
"""
import json, math, subprocess, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS, SR = 1080, 1920, 50, 48000
FONT_DIRS = [Path.home() / "Library/Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts"),
             Path("/System/Library/Fonts/Supplemental"), Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
             Path("C:/Windows/Fonts")]
# шрифт студии, если стоит → Inter (ставит tools/install.py) → системный гротеск
FONT_BOLD_NAMES = ["TT-Firs-Text-ExtraBold.ttf", "Inter-ExtraBold.ttf", "Inter-ExtraBold.otf", "Inter-Bold.ttf",
                   "InterVariable.ttf", "HelveticaNeue.ttc", "Arial Bold.ttf", "arialbd.ttf"]
FONT_MED_NAMES = ["TT-Firs-Text-DemiBold.ttf", "Inter-SemiBold.ttf", "Inter-SemiBold.otf", "Inter-Medium.ttf",
                  "InterVariable.ttf", "HelveticaNeue.ttc", "Arial.ttf", "arial.ttf"]


def find_font(names):
    for n in names:
        for d in FONT_DIRS:
            if (d / n).exists():
                return str(d / n)
    raise SystemExit("нет шрифта для титров: поставь Inter (tools/install.py) или задай путь в спеке")


FONT_BOLD = find_font(FONT_BOLD_NAMES)
FONT_MED = find_font(FONT_MED_NAMES)
ACCENT = (242, 163, 60, 255)
WHITE = (255, 255, 255, 255)


def ease_out(x):
    return 1 - (1 - x) ** 3


def ease_in(x):
    return x ** 3


def font(path, size):
    return ImageFont.truetype(path, size)


def wrap(draw, words, f, maxw):
    """Слова → строки, не шире maxw."""
    lines, cur = [], []
    for w in words:
        t = " ".join(cur + [w])
        if draw.textlength(t, font=f) <= maxw or not cur:
            cur.append(w)
        else:
            lines.append(cur)
            cur = [w]
    if cur:
        lines.append(cur)
    return lines


def draw_title(img, t, item):
    """Титр: слова вылетают снизу по очереди, уходят вместе."""
    t0, t1 = item["t"], item["t"] + item["dur"]
    if not (t0 - 0.05 <= t <= t1 + 0.3):
        return
    d = ImageDraw.Draw(img)
    size = item.get("size", 96)
    f = font(FONT_BOLD if item.get("weight", "bold") == "bold" else FONT_MED, size)
    words = item["text"].split()
    accent = set(item.get("accent", []))
    lines = wrap(d, words, f, item.get("maxw", W - 150))
    lh = int(size * 1.12)
    total = lh * len(lines)
    base_y = item.get("y", H - 520) - total // 2

    out_k = 0.0
    if t > t1:
        out_k = min((t - t1) / 0.18, 1.0)
    idx = 0
    for li, line in enumerate(lines):
        widths = [d.textlength(w + " ", font=f) for w in line]
        x = (W - (sum(widths) - d.textlength(" ", font=f))) / 2
        for wi, word in enumerate(line):
            k = (t - (t0 + idx * item.get("stagger", 0.06))) / 0.22
            idx += 1
            if k <= 0:
                continue
            a = ease_out(min(k, 1.0))
            dy = (1 - a) * 55
            alpha = a * (1 - ease_in(out_k))
            if alpha <= 0.01:
                continue
            col = ACCENT if word.strip(".,!?—:«»").upper() in accent else WHITE
            col = col[:3] + (int(255 * alpha),)
            y = base_y + li * lh + dy - out_k * 30
            # мягкая тень, чтобы читалось на любом кадре
            d.text((x + 3, y + 4), word, font=f, fill=(0, 0, 0, int(150 * alpha)))
            d.text((x, y), word, font=f, fill=col)
            x += widths[wi]


def draw_card(img, t, card):
    """Финальная карточка: заголовок, имя, подпись."""
    d = ImageDraw.Draw(img)
    t0 = card["t"]
    k = min(max((t - t0) / 0.5, 0), 1)
    a = ease_out(k)
    f1, f2, f3 = font(FONT_BOLD, 118), font(FONT_MED, 52), font(FONT_MED, 40)
    lines = wrap(d, card["title"].split(), f1, W - 160)
    y = H // 2 - 170
    for line in lines:
        txt = " ".join(line)
        w = d.textlength(txt, font=f1)
        d.text(((W - w) / 2, y + (1 - a) * 40), txt, font=f1, fill=WHITE[:3] + (int(255 * a),))
        y += 130
    y += 24
    for txt, f, col in ((card["name"], f2, ACCENT), (card["sub"], f3, (180, 180, 180, 255))):
        k2 = min(max((t - t0 - 0.18) / 0.5, 0), 1)
        a2 = ease_out(k2)
        w = d.textlength(txt, font=f)
        d.text(((W - w) / 2, y + (1 - a2) * 30), txt, font=f, fill=col[:3] + (int(col[3] * a2),))
        y += int(f.size * 1.7)


def render_overlay(spec, out):
    n = int(round(spec["duration"] * FPS))
    p = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
                          "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                          "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
                          str(out)], stdin=subprocess.PIPE)
    blank = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for i in range(n):
        t = i / FPS
        live = [x for x in spec["titles"] if x["t"] - 0.1 <= t <= x["t"] + x["dur"] + 0.4]
        if not live:
            p.stdin.write(blank.tobytes())
            continue
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for item in live:
            draw_title(img, t, item)
        p.stdin.write(img.tobytes())
    p.stdin.close()
    p.wait()
    return out


def render_endcard(card, dur, out):
    n = int(round(dur * FPS))
    p = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
                          "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                          "-c:v", "prores_ks", "-profile:v", "hq", "-pix_fmt", "yuv422p10le",
                          str(out)], stdin=subprocess.PIPE)
    for i in range(n):
        img = Image.new("RGBA", (W, H), (10, 10, 12, 255))
        draw_card(img, i / FPS, {**card, "t": 0.0})
        p.stdin.write(img.tobytes())
    p.stdin.close()
    p.wait()
    return out


# ---------- звук ----------
def whoosh(dur=0.45, f0=180, f1=5200, sr=SR):
    n = int(dur * sr)
    t = np.arange(n) / sr
    noise = np.random.default_rng(7).normal(0, 1, n)
    # полосовой свип: быстрая смена центра полосы даёт «пролёт»
    k = (t / dur)
    out = np.zeros(n)
    buf = 0.0
    for i in range(n):
        fc = f0 * (f1 / f0) ** k[i]
        a = math.exp(-2 * math.pi * fc / sr)
        buf = (1 - a) * noise[i] + a * buf
        out[i] = noise[i] - buf
    env = np.sin(np.pi * k) ** 1.6
    return out * env * 0.5


def impact(dur=0.7, f=62, sr=SR):
    n = int(dur * sr)
    t = np.arange(n) / sr
    sub = np.sin(2 * np.pi * (f * (1 - 0.55 * t / dur)) * t) * np.exp(-5 * t / dur)
    click = np.random.default_rng(3).normal(0, 1, n) * np.exp(-90 * t)
    return (sub * 0.85 + click * 0.18)


def riser(dur=2.5, sr=SR):
    n = int(dur * sr)
    t = np.arange(n) / sr
    k = t / dur
    tone = np.sin(2 * np.pi * (220 * (1 + 2.6 * k ** 2)) * t) * 0.18
    noise = np.random.default_rng(11).normal(0, 1, n) * 0.1 * k ** 2
    return (tone + noise) * (k ** 1.4)


def render_sfx(spec, out):
    n = int(spec["duration"] * SR) + SR
    mix = np.zeros(n)
    def put(x, at, g=1.0):
        i = int(at * SR)
        if i < 0:
            x, i = x[-i:], 0
        mix[i:i + len(x)] += x[: max(n - i, 0)] * g
    for ev in spec["sfx"]:
        kind, at = ev["kind"], ev["t"]
        g = ev.get("gain", 1.0)
        if kind == "whoosh":
            put(whoosh(ev.get("dur", 0.45)), at - 0.3, 0.55 * g)
        elif kind == "impact":
            put(impact(ev.get("dur", 0.7)), at, 0.8 * g)
        elif kind == "riser":
            put(riser(ev.get("dur", 2.5)), at, 0.7 * g)
    peak = float(np.max(np.abs(mix))) or 1.0
    mix = (mix / peak) * 0.72
    raw = out.with_suffix(".f32")
    mix.astype(np.float32).tofile(raw)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1",
                    "-i", str(raw), "-c:a", "pcm_s24le", str(out)], check=True)
    raw.unlink()
    return out


def main(proj, spec_path, out_dir):
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    print("оверлей…", flush=True)
    render_overlay(spec, out / "overlay.mov")
    print("концовка…", flush=True)
    render_endcard(spec["endcard"], spec["endcard"]["dur"], out / "endcard.mov")
    print("саунд-дизайн…", flush=True)
    render_sfx(spec, out / "sfx.wav")
    for f in ("overlay.mov", "endcard.mov", "sfx.wav"):
        q = out / f
        print(f"  {f}: {q.stat().st_size / 1e6:.1f} МБ")


if __name__ == "__main__":
    v = sys.argv
    if len(v) < 2:
        raise SystemExit(__doc__)
    g = lambda k, d=None: (v[v.index(k) + 1] if k in v else d)
    main(v[1], g("--spec"), g("--out"))
