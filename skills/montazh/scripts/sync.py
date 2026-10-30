"""Синхра P8 ↔ камеры: смещение и дрейф по кросс-корреляции (GCC-PHAT).

  python3 sync.py <проект> [--ref <файл-камера>] [--mic <файл-микс P8>] [--points 8] [--win 60] [--search 5]

Берёт N окон по всей записи, в каждом считает лаг между звуком камеры и звуком P8
(16 кГц моно, полоса 150–4000 Гц, фазовая корреляция), отбрасывает окна со слабым пиком и
строит линейную модель  mic = cam + a + b·cam  (a — смещение, b — ход часов рекордера).

Пишет 00_db/sync.json и тревогу, если пик низкий или модель не ложится.
Звук не меняет: всё считается на служебных копиях.
"""
import json, subprocess, sys
from pathlib import Path
import numpy as np
from common import alert, db, ensure, fmt_t, journal, now, read_jsonl, write_json

SR = 16000
MIN_Q = 2.0          # пик / второй пик — ниже считаем окно ненадёжным
COARSE = 30.0        # диапазон грубого поиска, с
FINE = 1.0           # диапазон точного поиска вокруг грубого, с


def grab(path, t, dur, sr=SR):
    """Кусок звука файла → float32 моно, полоса 150–4000 Гц."""
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-t", f"{dur:.3f}",
                          "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(sr),
                          "-af", "highpass=f=150,lowpass=f=4000", "-f", "f32le", "-"], capture_output=True)
    return np.frombuffer(out.stdout, dtype=np.float32).copy()


def gcc_phat(a, b, max_lag):
    """Лаг (в сэмплах) сигнала a относительно b: a[i] ≈ b[i-lag]. Качество = пик / второй пик."""
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    A, B = np.fft.rfft(a, n), np.fft.rfft(b, n)
    X = A * np.conj(B)
    cc = np.abs(np.fft.irfft(X / (np.abs(X) + 1e-12), n))
    cc = np.concatenate([cc[-max_lag:], cc[:max_lag + 1]])
    k = int(np.argmax(cc))
    guard = int(0.02 * SR)                       # второй пик ищем не ближе 20 мс
    rest = np.concatenate([cc[:max(k - guard, 0)], cc[k + guard:]])
    q = float(cc[k] / (rest.max() + 1e-12)) if rest.size else 0.0
    return k - max_lag, q


def lag_at(mic, ref, t, win, centre, search):
    """Смещение P8 относительно камеры в окне [t, t+win], поиск вокруг centre ±search."""
    c = grab(ref, t, win)
    m = grab(mic, t + centre - search, win + 2 * search)
    if len(c) < SR * 5 or len(m) < SR * 5:
        return None, 0.0
    lag, q = gcc_phat(m, c, int(2 * search * SR))
    return lag / SR - search + centre, q


def main(proj, ref=None, mic=None, points=8, win=60.0, search=5.0):
    root = Path(proj).resolve()
    a_dir = ensure(root)
    files = read_jsonl(db(root) / "files.jsonl")

    if ref is None:
        cams = [r for r in files if r.get("role") in ("atem_iso", "atem_program") and r.get("duration")]
        if not cams:
            raise SystemExit("нет камер в files.jsonl")
        ref_rec = sorted(cams, key=lambda r: (r.get("cam") or 99))[0]
        ref = root / ref_rec["path"]
        ref_dur = ref_rec["duration"]
    else:
        ref = Path(ref)
        ref_dur = None
    mic = Path(mic) if mic else a_dir / "03_transcript" / "mix16k.wav"
    if not mic.exists():
        raise SystemExit(f"нет микса P8 {mic} — сначала transcribe.py")
    if ref_dur is None:
        ref_dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                        "-of", "csv=p=0", str(ref)], capture_output=True, text=True).stdout)
    mic_dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", str(mic)], capture_output=True, text=True).stdout)
    print(f"камера: {ref.name} ({fmt_t(ref_dur)})\nP8:     {mic.name} ({fmt_t(mic_dur)})", flush=True)

    span = min(ref_dur, mic_dur)

    # грубый проход: длинное окно в середине, широкий поиск
    coarse, qc = lag_at(mic, ref, span * 0.4, 300.0, 0.0, search)
    if coarse is None:
        raise SystemExit("не смог прочитать звук для грубого прохода")
    print(f"грубо: {coarse * 1000:+.0f} мс (качество {qc:.1f})", flush=True)

    ts = [span * (i + 0.5) / points for i in range(points)]
    res = []
    for t in ts:
        off, q = lag_at(mic, ref, t, win, coarse, FINE)
        if off is None:
            continue
        res.append({"t": round(t, 1), "offset": round(off, 4), "q": round(q, 2)})
        print(f"  {fmt_t(t):>9}  смещение {off * 1000:+8.1f} мс  качество {q:5.2f}"
              f"{'  ← слабый пик' if q < MIN_Q else ''}", flush=True)

    good = [r for r in res if r["q"] >= MIN_Q]
    if len(good) < 3:
        alert(root, "sound", f"Синхра ненадёжна: уверенных окон {len(good)} из {len(res)}", str(ref.name), run=now())
        print("\n⚠ уверенных окон мало — модель не строю")
        write_json(db(root) / "sync.json", {"run": now(), "ref": ref.name, "mic": mic.name, "points": res,
                                            "ok": False})
        return

    x = np.array([r["t"] for r in good])
    y = np.array([r["offset"] for r in good])
    b, a = np.polyfit(x, y, 1)
    resid = y - (a + b * x)
    rms_ms = float(np.sqrt((resid ** 2).mean()) * 1000)
    drift_ppm = b * 1e6
    mid = a + b * (span / 2)

    out = {"run": now(), "ref": str(ref.relative_to(root)) if ref.is_relative_to(root) else str(ref),
           "mic": str(mic.relative_to(root)), "model": "mic_time = cam_time + a + b*cam_time",
           "a": round(float(a), 4), "b": float(b), "drift_ppm": round(float(drift_ppm), 1),
           "offset_at_middle": round(float(mid), 4), "residual_rms_ms": round(rms_ms, 1),
           "span": round(span, 1), "points": res, "used": len(good), "ok": True}
    write_json(db(root) / "sync.json", out)

    print(f"\nМодель: смещение {a * 1000:+.1f} мс в начале, ход часов {drift_ppm:+.1f} ppm "
          f"(за {fmt_t(span)} набегает {b * span * 1000:+.1f} мс)")
    print(f"Разброс вокруг модели: {rms_ms:.1f} мс · уверенных окон {len(good)}/{len(res)}")
    print(f"Для v00 одним клипом: сдвиг по середине записи {mid * 1000:+.1f} мс "
          f"({mid * 50:+.2f} кадра при 50p), края разойдутся на ±{abs(b) * span / 2 * 1000:.1f} мс")
    if rms_ms > 25:
        alert(root, "sound", f"Синхра: разброс вокруг модели {rms_ms:.0f} мс — проверь на слух", ref.name, run=now())
    if abs(drift_ppm) > 60:
        alert(root, "sound", f"Синхра: ход часов {drift_ppm:+.0f} ppm — больше обычного, класть кусками", ref.name,
              run=now())
    seq = journal(root, "sync", a=round(float(a), 4), drift_ppm=round(float(drift_ppm), 1),
                  residual_ms=round(rms_ms, 1), used=len(good), points=len(res))
    print(f"\n[seq {seq}] → 00_db/sync.json")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    v = sys.argv
    g = lambda k, d, cast=str: (cast(v[v.index(k) + 1]) if k in v else d)
    main(v[1], ref=g("--ref", None), mic=g("--mic", None),
         points=g("--points", 8, int), win=g("--win", 60.0, float), search=g("--search", 5.0, float))
