"""Расшифровка микса P8 + кто говорит (по энергии микрофонов).

  python3 transcribe.py <проект> [--model <repo>] [--tracks MIC5.WAV MIC6.WAV] [--lang ru]

Делает: микс дорожек в 16 кГц моно → mlx-whisper со словными таймкодами → для каждого сегмента
и слова определяет говорящего по тому, чей микрофон громче (дорожки одного P8 сэмпл-в-сэмпл).
Если есть нарезка оператора (atem_cuts.jsonl) — сопоставляет микрофоны с камерами.

Пишет: 00_db/transcript/mix.segments.jsonl, mix.words.jsonl, 00_db/speakers.json,
03_transcript/mix16k.wav, mix.txt (для чтения смысла), cache/env_<дорожка>.npy (огибающие 100 Гц).
Исходники только читает. Звук не меняет: микс — служебный файл для распознавания.
"""
import json, subprocess, sys, time
from pathlib import Path
import numpy as np
from common import db, ensure, fmt_t, journal, now, read_json, read_jsonl, write_json

HOP = 0.01          # шаг огибающей, с
SR = 16000
MODEL = "mlx-community/whisper-large-v3-turbo"


def decode_mono(path, sr=SR):
    """Файл → numpy float32 моно."""
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
                          "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True)
    return np.frombuffer(out.stdout, dtype=np.float32)


def envelope(x, sr=SR, hop=HOP):
    """RMS по окнам hop → дБ."""
    n = int(sr * hop)
    m = len(x) // n
    e = np.sqrt((x[:m * n].reshape(m, n) ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(e)


def main(proj, model=MODEL, tracks=None, lang="ru"):
    root = Path(proj).resolve()
    a = ensure(root)
    t_start = time.time()
    files = read_jsonl(db(root) / "files.jsonl")

    if tracks:
        paths = [root / t if (root / t).exists() else Path(t) for t in tracks]
        names = [p.stem for p in paths]
    else:
        p8 = [r for r in files if r.get("role") == "p8_track"]
        if not p8:
            p8 = [r for r in files if r.get("role") == "atem_audio" and "MIC" in r.get("src", "")]
        if not p8:
            raise SystemExit("не нашёл дорожек микрофонов в files.jsonl")
        paths = [root / r["path"] for r in p8]
        names = [r.get("ch") or r.get("src") or Path(r["path"]).stem for r in p8]
    print(f"дорожки: {', '.join(f'{n} ({p.name})' for n, p in zip(names, paths))}", flush=True)

    # --- огибающие и микс ---
    sig = []
    for n, p in zip(names, paths):
        cache = a / "cache" / f"env_{n}.npy"
        x = decode_mono(p)
        sig.append(x)
        np.save(cache, envelope(x))
        print(f"  {n}: {len(x) / SR / 60:.1f} мин, огибающая → {cache.name}", flush=True)
    L = min(len(s) for s in sig)
    envs = np.stack([np.load(a / "cache" / f"env_{n}.npy")[: L // int(SR * HOP)] for n in names])

    mix_path = a / "03_transcript" / "mix16k.wav"
    mix = np.sum([s[:L] for s in sig], axis=0)
    peak = float(np.max(np.abs(mix))) or 1.0
    (mix / peak * 0.89).astype(np.float32).tofile("/tmp/_mix.f32")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1",
                    "-i", "/tmp/_mix.f32", str(mix_path)], check=True)
    print(f"микс → {mix_path.name} ({L / SR / 60:.1f} мин)", flush=True)

    # --- распознавание ---
    import mlx_whisper
    print(f"распознаю моделью {model} …", flush=True)
    t0 = time.time()
    res = mlx_whisper.transcribe(str(mix_path), path_or_hf_repo=model, language=lang, word_timestamps=True,
                                 condition_on_previous_text=False, hallucination_silence_threshold=2.0,
                                 verbose=None)
    print(f"распознано за {time.time() - t0:.0f} с", flush=True)

    # --- кто говорит: чей микрофон громче в окне ---
    def who(t1, t2):
        i, j = int(t1 / HOP), max(int(t2 / HOP), int(t1 / HOP) + 1)
        w = envs[:, i:j]
        if w.size == 0:
            return None, []
        lv = [float(np.percentile(r, 80)) for r in w]       # 80-й перцентиль: без вспышек
        k = int(np.argmax(lv))
        return names[k], [round(v, 1) for v in lv]

    seg_out, word_out = [], []
    for s in res["segments"]:
        sp, lv = who(s["start"], s["end"])
        seg_out.append({"t": round(s["start"], 3), "end": round(s["end"], 3), "mic": sp,
                        "levels": lv, "text": s["text"].strip()})
        for w in s.get("words", []) or []:
            wsp, _ = who(w["start"], w["end"])
            word_out.append({"t": round(w["start"], 3), "end": round(w["end"], 3), "mic": wsp,
                             "w": w["word"].strip(), "p": round(w.get("probability", 0), 3)})

    td = db(root) / "transcript"
    with open(td / "mix.segments.jsonl", "w", encoding="utf-8") as f:
        for r in seg_out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(td / "mix.words.jsonl", "w", encoding="utf-8") as f:
        for r in word_out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # текст для чтения смысла
    lines, cur, buf = [], None, []
    for s in seg_out:
        if s["mic"] != cur:
            if buf:
                lines.append(f"[{fmt_t(buf[0]['t'])}] {cur}: " + " ".join(x["text"] for x in buf))
            cur, buf = s["mic"], [s]
        else:
            buf.append(s)
    if buf:
        lines.append(f"[{fmt_t(buf[0]['t'])}] {cur}: " + " ".join(x["text"] for x in buf))
    (a / "03_transcript" / "mix.txt").write_text("\n\n".join(lines) + "\n", encoding="utf-8")

    # --- микрофон ↔ камера по нарезке оператора ---
    cams = {}
    cuts = read_jsonl(db(root) / "atem_cuts.jsonl")
    for sh in cuts:
        if not sh.get("cam") or sh["dur"] < 2:
            continue
        sp, _ = who(sh["t"], sh["t"] + sh["dur"])
        if sp:
            cams.setdefault(sh["cam"], {}).setdefault(sp, 0.0)
            cams[sh["cam"]][sp] += sh["dur"]

    talk = {n: round(float(((envs[k] > np.percentile(envs[k], 20) + 12).sum()) * HOP), 1)
            for k, n in enumerate(names)}
    speakers = {"run": now(), "tracks": {n: str(p.relative_to(root)) for n, p in zip(names, paths)},
                "speech_seconds": talk,
                "cam_mic": {str(c): {k: round(v, 1) for k, v in sorted(d.items(), key=lambda kv: -kv[1])}
                            for c, d in sorted(cams.items())},
                "note": "mic ↔ человек: имена проставляет монтажёр или расшифровка"}
    write_json(db(root) / "speakers.json", speakers)

    print("\n| дорожка | речь | сегментов |\n|---|---|---|")
    for n in names:
        print(f"| {n} | {fmt_t(talk[n])} | {sum(1 for s in seg_out if s['mic'] == n)} |")
    if cams:
        print("\n| камера оператора | чей микрофон звучит |\n|---|---|")
        for c, d in speakers["cam_mic"].items():
            print(f"| CAM {c} | " + ", ".join(f"{k} {fmt_t(v)}" for k, v in d.items()) + " |")
    seq = journal(root, "transcribe", model=model, segments=len(seg_out), words=len(word_out),
                  seconds=round(time.time() - t_start, 1))
    print(f"\n[seq {seq}] → 00_db/transcript/mix.segments.jsonl ({len(seg_out)}), mix.words.jsonl ({len(word_out)}), 03_transcript/mix.txt")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    a = sys.argv
    tr = None
    if "--tracks" in a:
        i = a.index("--tracks") + 1
        tr = [x for x in a[i:] if not x.startswith("--")]
    main(a[1], model=(a[a.index("--model") + 1] if "--model" in a else MODEL), tracks=tr,
         lang=(a[a.index("--lang") + 1] if "--lang" in a else "ru"))
