"""Исследование, быстрый проход (1–2 мин).  python3 scan.py <проект> [--no-sheets]

Опись файлов → ffprobe → роли (ATEM ISO / программа / звук ATEM / .drp / P8 дорожки / P8 микс / OBS / документы)
→ съёмки и записи → сценарий S1–S6 → уровни дорожек P8 → контакт-листы камер → тревоги.
Пишет: 00_db/files.jsonl, recordings.json, project.json, alerts.jsonl, 01_intake/REPORT.md, brief.md, журнал.
Исходники только читает. Кэш ffprobe — _agent/cache/probe.json.
"""
import re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from common import (SKIP_DIRS, agent, alert, db, ensure, ffprobe, fmt_t, journal, now, read_json, write_json)

VIDEO = {".mp4", ".mov", ".mxf", ".mkv", ".m4v", ".avi"}
AUDIO = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".aif", ".aiff"}
DOCS = {".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".pptx", ".key", ".pages"}
IMAGES = {".png", ".jpg", ".jpeg", ".heic", ".tif", ".tiff", ".psd"}

RE_ISO = re.compile(r"^(?P<prefix>.*?)\s*CAM\s*(?P<cam>\d+)(?:\s+(?P<part>\d{2}))?$", re.I)
RE_PROG = re.compile(r"^(?P<prefix>.+?)\s+(?P<part>\d{2})$")
RE_P8 = re.compile(r"^(?P<ch>MIC\s?\d|PHONE|SOUND_PAD|USB)(?:\s+(?P<part>\d{2}))?$", re.I)
RE_P8MIX = re.compile(r"^\d{4}_\d{4}_\d{4}")
RE_OBS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}")
RE_ATEM_AUD = re.compile(r"^(?P<prefix>.+?)\s+(?P<src>(?:CAM|MIC)\s*\d+)(?:\s+(?P<part>\d{2}))?$", re.I)


def walk(root):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if p.is_file() and not p.name.startswith("._"):
            yield p


def source_root(root, p):
    """Ближайшая папка source/ — граница одной съёмки; иначе папка файла."""
    for anc in p.parents:
        if anc == root.parent:
            break
        if anc.name.lower() == "source":
            return anc
    return p.parent


def probe_cached(cache, p):
    st = p.stat()
    key = f"{p}|{st.st_size}|{int(st.st_mtime)}"
    if key not in cache:
        cache[key] = ffprobe(p)
    return cache[key]


def summarize(pr):
    if not pr:
        return {"probe_error": True}
    fmt = pr.get("format", {})
    out = {"duration": round(float(fmt.get("duration", 0) or 0), 3)}
    for s in pr.get("streams", []):
        tags = s.get("tags", {}) or {}
        if s.get("codec_type") == "video" and "width" not in out:
            num, den = (s.get("r_frame_rate") or "0/1").split("/")
            out.update(vcodec=s.get("codec_name"), width=s.get("width"), height=s.get("height"),
                       fps=round(float(num) / float(den or 1), 3) if float(den or 1) else None)
            if tags.get("timecode"):
                out["timecode"] = tags["timecode"]
        elif s.get("codec_type") == "audio" and "sr" not in out:
            out.update(acodec=s.get("codec_name"), sr=int(s.get("sample_rate", 0) or 0), ch=s.get("channels"),
                       bits=s.get("bits_per_sample") or s.get("bits_per_raw_sample"))
        if tags.get("timecode") and "timecode" not in out:
            out["timecode"] = tags["timecode"]
    ct = (fmt.get("tags") or {}).get("creation_time")
    if ct:
        out["creation_time"] = ct
    return out


def drp_format(p):
    head = p.read_bytes()[:2048]
    if head[:2] == b"PK":
        return "zip"
    try:
        import json
        json.loads(head.decode("utf-8", "ignore").splitlines()[0])
        return "json_lines"
    except Exception:
        return "xml" if head.lstrip().startswith(b"<") else "binary"


def classify(p, info):
    ext, stem, path_s = p.suffix.lower(), p.stem, str(p)
    if ext == ".drp":
        return {"role": "atem_project", "drp_format": drp_format(p)}
    if p.name == "result.json" and "chatexport" in path_s.lower():
        return {"role": "chat"}
    if ext in VIDEO:
        m = RE_ISO.match(stem)
        if m:
            return {"role": "atem_iso", "prefix": m["prefix"].strip(), "cam": int(m["cam"]), "part": m["part"] or "01"}
        if RE_OBS.match(stem):
            return {"role": "obs"}
        m = RE_PROG.match(stem)
        if m:
            return {"role": "video_numbered", "prefix": m["prefix"].strip(), "part": m["part"]}
        return {"role": "video"}
    if ext in AUDIO:
        sr = info.get("sr") or 0
        if "p8_multitrack" in path_s.lower():
            m = RE_P8.match(stem)
            return {"role": "p8_track", "ch": (m["ch"].upper().replace(" ", "") if m else stem), "part": p.parent.name}
        m = RE_P8.match(stem)
        if m and sr != 48000:
            return {"role": "p8_track", "ch": m["ch"].upper().replace(" ", ""), "part": m["part"] or "01"}
        if RE_P8MIX.match(stem):
            return {"role": "p8_mix", "part": stem[:14]}
        m = RE_ATEM_AUD.match(stem)
        if m and sr == 48000:
            return {"role": "atem_audio", "prefix": m["prefix"].strip(), "src": m["src"].upper().replace(" ", " "),
                    "part": m["part"] or "01"}
        if m and RE_P8.match(stem):
            return {"role": "p8_track", "ch": m["src"].upper().replace(" ", ""), "part": m["part"] or "01"}
        return {"role": "audio"}
    if ext in DOCS:
        return {"role": "doc"}
    if ext in IMAGES:
        return {"role": "image"}
    return {"role": "other"}


def levels(p):
    """Пик, RMS и доля речи (по silencedetect −45 дБ) — по первому каналу, 8 кГц."""
    cmd = ["ffmpeg", "-nostdin", "-v", "info", "-i", str(p), "-map", "0:a:0", "-ac", "1",
           "-af", "aresample=8000,astats=measure_perchannel=none:measure_overall=Peak_level+RMS_level,"
                  "silencedetect=noise=-45dB:d=0.5", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True).stderr
    peak = re.findall(r"Peak level dB:\s*(-?[\d.inf]+)", err)
    rms = re.findall(r"RMS level dB:\s*(-?[\d.inf]+)", err)
    sil = sum(float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", err))
    num = lambda v: round(float(v), 1) if v and "inf" not in v else -120.0
    return {"peak_db": num(peak[-1] if peak else None), "rms_db": num(rms[-1] if rms else None), "silence_s": round(sil, 1)}


def contact_sheet(p, out, dur):
    """12 кадров на равных интервалах (быстрый переход -ss к каждому) → одна картинка 4×3."""
    tmp = out.with_suffix("")
    tmp.mkdir(exist_ok=True)
    for i in range(12):
        t = dur * (i + 0.5) / 12
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(p), "-frames:v", "1",
                        "-vf", "scale=384:-2", "-q:v", "4", str(tmp / f"{i:02d}.jpg")], capture_output=True)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(tmp / "%02d.jpg"),
                    "-vf", "tile=4x3", "-frames:v", "1", "-q:v", "4", str(out)], capture_output=True)
    for f in tmp.glob("*.jpg"):
        f.unlink()
    tmp.rmdir()
    return out.exists()


def main(proj, sheets=True):
    root = Path(proj).resolve()
    t0 = time.time()
    a = ensure(root)
    run = now()
    cache_path = a / "cache" / "probe.json"
    cache = read_json(cache_path, {})

    files = list(walk(root))
    media = [p for p in files if p.suffix.lower() in VIDEO | AUDIO]
    with ThreadPoolExecutor(6) as ex:
        probes = dict(zip(media, ex.map(lambda p: probe_cached(cache, p), media)))
    write_json(cache_path, cache)

    recs = []
    for p in files:
        info = summarize(probes[p]) if p in probes else {}
        rec = {"path": str(p.relative_to(root)), "size": p.stat().st_size, **info, **classify(p, info),
               "shoot": str(source_root(root, p).relative_to(root))}
        recs.append(rec)

    # уровни дорожек P8 (и прочих WAV, если P8 нет)
    tracks = [r for r in recs if r["role"] in ("p8_track", "p8_mix", "atem_audio")]
    with ThreadPoolExecutor(6) as ex:
        for r, lv in zip(tracks, ex.map(lambda r: levels(root / r["path"]), tracks)):
            r.update(lv)
            d = r.get("duration") or 0
            r["speech_pct"] = round(100 * max(0.0, 1 - r["silence_s"] / d), 1) if d else None

    # контакт-листы камер
    sheet_dir = a / "01_intake" / "sheets"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    cams = [r for r in recs if r["role"] in ("atem_iso", "video", "video_numbered", "obs") and r.get("duration")]
    if sheets and cams:
        def mk(r):
            out = sheet_dir / (re.sub(r"[^\w.-]+", "_", r["path"]) + ".jpg")
            r["sheet"] = str(out.relative_to(root)) if contact_sheet(root / r["path"], out, r["duration"]) else None
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(mk, cams))

    with open(db(root) / "files.jsonl", "w", encoding="utf-8") as f:
        import json
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---- съёмки и записи ----
    shoots = {}
    for r in recs:
        shoots.setdefault(r["shoot"], []).append(r)
    av_shoots = {k: v for k, v in shoots.items() if any(x["role"] in ("atem_iso", "p8_track", "p8_mix", "video", "obs")
                                                         for x in v)}
    alerts = []

    def warn(kind, text, where=None):
        alerts.append((kind, text, where))
        alert(root, kind, text, where, run=run)

    result = {"run": run, "shoots": []}
    for sk, items in av_shoots.items():
        iso = [x for x in items if x["role"] == "atem_iso"]
        prog = [x for x in items if x["role"] == "video_numbered" and any(x["prefix"] == i["prefix"] for i in iso)]
        for x in prog:
            x["role"] = "atem_program"
        drp = [x for x in items if x["role"] == "atem_project"]
        p8 = [x for x in items if x["role"] == "p8_track"]
        p8mix = [x for x in items if x["role"] == "p8_mix"]
        atem_aud = [x for x in items if x["role"] == "atem_audio"]
        other_v = [x for x in items if x["role"] in ("video", "video_numbered")]
        obs = [x for x in items if x["role"] == "obs"]

        parts = {}
        for x in iso:
            parts.setdefault(x["part"], {"cams": {}, "p8": {}})["cams"][x["cam"]] = x
        p8_parts = {}
        for x in p8:
            p8_parts.setdefault(x["part"], {})[x["ch"]] = x

        # сценарий
        if iso and (p8 or p8mix):
            sc = "S1" if p8 else "S1-mix"
        elif iso:
            sc = "S2"
        elif (p8 or p8mix) and other_v:
            sc = "S4"
        elif p8 or p8mix:
            sc = "S3"
        elif obs:
            sc = "S5"
        else:
            sc = "S6"
        shoot = {"shoot": sk, "scenario": sc, "atem_parts": {}, "p8_parts": {}, "drp": [d["path"] for d in drp],
                 "other_video": [v["path"] for v in other_v + obs]}

        # тревоги
        if iso and not (p8 or p8mix):
            warn("sound", "Нет записи P8 — звук только с ATEM/камер", sk)
        if p8mix and not p8:
            warn("sound", "P8 только стерео-микс, дорожек по микрофонам нет — спикеров не разделить", sk)
        if iso and not drp:
            warn("picture", "Нет файла .drp от ATEM — нет нарезки оператора", sk)
        for part, pd in sorted(parts.items()):
            durs = {c: x["duration"] for c, x in pd["cams"].items()}
            shoot["atem_parts"][part] = {"cams": {c: x["path"] for c, x in sorted(pd["cams"].items())},
                                         "durations": durs, "fps": sorted({x.get("fps") for x in pd["cams"].values()}),
                                         "size": sorted({f"{x.get('width')}x{x.get('height')}" for x in pd["cams"].values()})}
            if durs and max(durs.values()) - min(durs.values()) > 1.0:
                warn("picture", f"ISO-файлы части {part} разной длины: " +
                     ", ".join(f"CAM {c} {fmt_t(d)}" for c, d in sorted(durs.items())) + " — возможно, файл оборван", sk)
            nums = sorted(pd["cams"])
            missing = [n for n in range(1, max(nums) + 1) if n not in nums]
            if missing:
                warn("picture", f"Часть {part}: нет файлов камер {missing}", sk)
        for part, chs in sorted(p8_parts.items()):
            shoot["p8_parts"][part] = {ch: x["path"] for ch, x in sorted(chs.items())}
            for ch, x in chs.items():
                if x.get("speech_pct") is not None and (x["speech_pct"] < 2 or x.get("rms_db", -120) < -60):
                    warn("sound", f"{ch} ({part}) почти молчит: речь {x['speech_pct']}%, RMS {x.get('rms_db')} дБ", x["path"])
                if x.get("peak_db", -120) > -0.3:
                    warn("sound", f"{ch} ({part}) пик {x['peak_db']} дБ — возможен клиппинг", x["path"])
        cam_len = max((max(v["durations"].values()) for v in shoot["atem_parts"].values() if v["durations"]), default=0)
        p8_len = max((x["duration"] for x in p8), default=0)
        if cam_len and p8_len and p8_len < cam_len - 5:
            warn("sound", f"P8 ({fmt_t(p8_len)}) короче камер ({fmt_t(cam_len)}) — часть записи без звука P8? Уточнится синхрой", sk)
        result["shoots"].append(shoot)

    write_json(db(root) / "recordings.json", result)
    pj = read_json(db(root) / "project.json", {})
    pj.update(scanned=run, shoots=len(result["shoots"]),
              scenario=(result["shoots"][0]["scenario"] if len(result["shoots"]) == 1 else "несколько съёмок"))
    write_json(db(root) / "project.json", pj)

    # ---- отчёт ----
    L = [f"# Исследование: быстрый проход — {root.name}", "",
         f"{run[:16]} · {len(files)} файлов, медиа {len(media)} · {time.time() - t0:.0f} с", ""]
    if len(result["shoots"]) > 1:
        L += [f"**Съёмок в папке: {len(result['shoots'])}** — спросить, какую монтировать.", ""]
    for s in result["shoots"]:
        L += [f"## Съёмка `{s['shoot']}` · сценарий {s['scenario']}", ""]
        for part, ap in s["atem_parts"].items():
            L.append(f"**ATEM, часть {part}:** " + ", ".join(f"CAM {c} {fmt_t(ap['durations'][c])}" for c in ap["cams"]) +
                     f" · {', '.join(ap['size'])} · {', '.join(str(f) for f in ap['fps'])} fps")
        if s["drp"]:
            fmts = {r["path"]: r.get("drp_format") for r in recs if r["path"] in s["drp"]}
            L.append("**Проект ATEM:** " + ", ".join(f"`{k}` ({v})" for k, v in fmts.items()))
        if s["p8_parts"]:
            L += ["", "| P8 часть | дорожка | длит | RMS дБ | пик дБ | речь % |", "|---|---|---|---|---|---|"]
            for part, chs in s["p8_parts"].items():
                for ch, path in chs.items():
                    r = next(x for x in recs if x["path"] == path)
                    L.append(f"| {part} | {ch} | {fmt_t(r.get('duration'))} | {r.get('rms_db')} | {r.get('peak_db')} | {r.get('speech_pct')} |")
        if s["other_video"]:
            L += ["", "**Другое видео:** " + ", ".join(f"`{v}`" for v in s["other_video"][:20])]
        L.append("")
    if alerts:
        L += ["## Тревоги", ""] + [f"- **{k}** — {t}" + (f" (`{w}`)" if w else "") for k, t, w in alerts] + [""]
    else:
        L += ["## Тревоги", "", "Нет.", ""]
    sh = [r for r in recs if r.get("sheet")]
    if sh:
        L += ["## Контакт-листы (12 кадров по длине файла)", ""] + [f"- `{r['path']}` → `{r['sheet']}`" for r in sh] + [""]
    docs = [r for r in recs if r["role"] in ("doc", "chat", "image")]
    if docs:
        L += ["## Документы и картинки", ""] + [f"- `{r['path']}` ({r['role']})" for r in docs[:40]] + [""]
    L += ["## Не сделано в быстром проходе", "",
          "- синхра P8 ↔ камеры, расшифровка, кто где говорит, карта камер и миков;",
          "- заморозки и чёрные кадры (глубокая проверка), короткие склейки по логу ATEM;",
          "- главы, мысли, хайлайты, риски публикации."]
    (a / "01_intake" / "REPORT.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    brief = [f"# {root.name}", "", f"Сценарий: {pj.get('scenario')} · съёмок: {pj.get('shoots')} · тревог: {len(alerts)}",
             f"Быстрый проход: {run[:16]} → `_agent/01_intake/REPORT.md`", "",
             "Дальше: квиз «что делаем», медленный проход (синхра, расшифровка), v00."]
    (db(root) / "brief.md").write_text("\n".join(brief) + "\n", encoding="utf-8")
    seq = journal(root, "scan", files=len(files), media=len(media), shoots=len(result["shoots"]),
                  scenario=pj.get("scenario"), alerts=len(alerts), seconds=round(time.time() - t0, 1))
    print("\n".join(L))
    print(f"\n[seq {seq}] отчёт: {a / '01_intake' / 'REPORT.md'}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    main(sys.argv[1], sheets="--no-sheets" not in sys.argv)
