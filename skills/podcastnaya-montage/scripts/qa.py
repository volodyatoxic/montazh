"""Контроль качества версии: структура таймлайна, синхра, стыки по звуку, сверка с разметкой.

  python3 qa.py <проект> --timeline "v01 · смысловой" [--no-render]

Порядок — от дешёвого к дорогому (references/qa.md):
 2. структура: дыры и наложения на V1, длина против плана, дорожки, смещение каждого куска P8 против модели;
 3. разметка: каждый красный маркер объяснён, главы по правилам YouTube, синие маркеры человека получили ответ;
 1. стыки: рендерим ТОЛЬКО звук таймлайна и смотрим огибающую вокруг каждой склейки — обрыв слова, щелчок.

Каждое ⚠ — строка в отчёте и жёлтый маркер на таймлайне. Видео не рендерим.
Пишет 07_qa/<версия>.md, 00_db/qa.jsonl, журнал.
"""
import json, re, subprocess, sys, time, unicodedata
from pathlib import Path
import numpy as np
from common import agent, connect, db, ensure, fmt_t, journal, now, read_json, append_jsonl

SR = 16000


def env_of(path, hop=0.01):
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a", "-ac", "1",
                          "-ar", str(SR), "-f", "f32le", "-"], capture_output=True)
    x = np.frombuffer(out.stdout, dtype=np.float32)
    n = int(SR * hop)
    m = len(x) // n
    e = np.sqrt((x[:m * n].reshape(m, n) ** 2).mean(axis=1) + 1e-12)
    return x, 20 * np.log10(e)


def main(proj, tl_name, do_render=True):
    root = Path(proj).resolve()
    ensure(root)
    edl = read_json(agent(root) / "04_edit" / "v01" / "edl.json", {})
    sync = read_json(db(root) / "sync.json", {})
    a, b = sync.get("a", 0), sync.get("b", 0)
    rows, warns = [], []

    def ok(level, what, detail=""):
        rows.append(("OK", level, what, detail))

    def bad(level, what, detail, t=None):
        rows.append(("⚠", level, what, detail))
        warns.append({"level": level, "what": what, "detail": detail, "t": t})

    r, pm, p = connect()
    tl = next((p.GetTimelineByIndex(i) for i in range(1, p.GetTimelineCount() + 1)
               if p.GetTimelineByIndex(i).GetName() == tl_name), None)
    if tl is None:
        raise SystemExit(f"нет таймлайна «{tl_name}»")
    p.SetCurrentTimeline(tl)
    fps = float(tl.GetSetting("timelineFrameRate"))
    start, end = tl.GetStartFrame(), tl.GetEndFrame()
    dur = (end - start) / fps
    print(f"проверяю «{tl_name}» · {fmt_t(dur)} · {fps:g} fps")

    # ---- 2. структура ----
    v = tl.GetItemListInTrack("video", 1) or []
    holes, overlaps, pos = [], [], start
    for it in v:
        if it.GetStart() > pos:
            holes.append(((pos - start) / fps, (it.GetStart() - pos) / fps))
        elif it.GetStart() < pos:
            overlaps.append((pos - start) / fps)
        pos = max(pos, it.GetEnd())
    ok("структура", "клипов на V1", str(len(v)))
    (ok if not holes else bad)("структура", "дыры на V1",
                               "нет" if not holes else "; ".join(f"{fmt_t(x)} ({y * 1000:.0f} мс)" for x, y in holes[:6]))
    (ok if not overlaps else bad)("структура", "наложения на V1",
                                  "нет" if not overlaps else ", ".join(fmt_t(x) for x in overlaps[:6]))
    want = edl.get("new_duration")
    if want:
        d = abs(dur - want)
        (ok if d <= 1 / fps + 1e-6 else bad)("структура", "длина против плана",
                                             f"{fmt_t(dur)} против {fmt_t(want)}, разница {d * 1000:.0f} мс")

    na = tl.GetTrackCount("audio")
    names = [tl.GetTrackName("audio", i) for i in range(1, na + 1)]
    enabled = [tl.GetIsTrackEnabled("audio", i) for i in range(1, na + 1)]
    ok("структура", "аудиодорожки", ", ".join(f"A{i + 1} {n}{'' if enabled[i] else ' (выкл)'}"
                                              for i, n in enumerate(names)))
    for i in range(1, na + 1):
        items = tl.GetItemListInTrack("audio", i) or []
        cov = sum(it.GetDuration() for it in items) / fps
        if not items:
            bad("структура", f"A{i} пустая", "нет клипов")
        elif abs(cov - dur) > 0.5:
            bad("структура", f"A{i} покрытие", f"{fmt_t(cov)} против {fmt_t(dur)}")
        else:
            ok("структура", f"A{i} покрытие", f"{fmt_t(cov)} · клипов {len(items)}")

    # синхра: смещение каждого куска P8 против модели
    worst = 0.0
    for i in range(1, na + 1):
        for it in tl.GetItemListInTrack("audio", i) or []:
            new_t = (it.GetStart() - start) / fps
            rec = it.GetStart() - start
            piece = min((x for x in edl.get("audio", []) if abs(x["rec"] - rec) <= 1),
                        key=lambda x: abs(x["rec"] - rec), default=None)
            if not piece:
                continue
            worst = max(worst, abs(it.GetSourceStartFrame() - piece["s"]) / fps)
    (ok if worst <= 1.5 / fps else bad)("синхра", "смещение кусков P8 против модели",
                                        f"худшее расхождение {worst * 1000:.0f} мс "
                                        f"({worst * fps:.2f} кадра, допуск 1,5 кадра)")

    # ---- 3. сверка с разметкой ----
    marks = tl.GetMarkers() or {}
    by_color = {}
    for f, m in marks.items():
        by_color.setdefault(m["color"], []).append((f, m))
    ok("разметка", "маркеров", ", ".join(f"{c} {len(v2)}" for c, v2 in sorted(by_color.items())))
    reds = by_color.get("Red", [])
    cuts = edl.get("cuts", [])
    inner = [c for c in cuts if c["t"] > 0.05 and c["end"] < edl.get("source_duration", 1e9) - 0.05]
    (ok if len(reds) >= len(cuts) - 2 else bad)("разметка", "красные маркеры на вырезах",
                                                f"{len(reds)} маркеров на {len(cuts)} вырезов")
    for f, m in reds:
        if not (m.get("note") or "").strip():
            bad("разметка", "красный без пояснения", m.get("name", ""), f / fps)
    blues = by_color.get("Blue", [])
    if blues:
        greens = by_color.get("Green", [])
        bad("разметка", "синие маркеры человека", f"{len(blues)} шт., зелёных ответов {len(greens)}")
    else:
        ok("разметка", "синие маркеры человека", "нет — это первая сдача")

    ch = sorted((f / fps, m["name"]) for f, m in by_color.get("Purple", []))
    if len(ch) < 3:
        bad("разметка", "главы", f"их {len(ch)}, YouTube требует минимум 3")
    elif ch[0][0] > 0.04:
        bad("разметка", "первая глава", f"начинается в {fmt_t(ch[0][0])}, а должна в 0:00")
    else:
        short = [(t, n) for (t, n), (t2, _) in zip(ch, ch[1:] + [(dur, "")]) if t2 - t < 10]
        (ok if not short else bad)("разметка", "главы",
                                   f"{len(ch)} шт., первая в 0:00" +
                                   ("" if not short else f", короче 10 с: {short}"))

    # ---- 1. стыки по звуку ----
    qa_dir = agent(root) / "07_qa"
    # Имя служебного рендера — только ASCII (стандарт хранения студии: диски exFAT).
    # И сравниваем имена нормализованными: macOS хранит их в NFD, поэтому «й» на диске —
    # это «и» + U+0306, и обычная маска с «й» в неё не попадает.
    nfc = lambda x: unicodedata.normalize("NFC", x)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_",
                  unicodedata.normalize("NFKD", tl_name).encode("ascii", "ignore").decode()).strip("_")
    stem = f"QA_{slug or 'timeline'}_audio"
    def found():
        return sorted((q for q in qa_dir.iterdir()
                       if nfc(q.name).startswith(nfc(stem)) and q.suffix.lower() != ".md"
                       and not q.name.startswith("._")),
                      key=lambda q: q.stat().st_mtime)
    wav = (found() or [qa_dir / stem])[-1]
    if do_render:
        print("рендерю только звук…", flush=True)
        for old_file in found():
            old_file.unlink()                      # свой прошлый прогон, чтобы не копить гигабайты
        r.OpenPage("deliver")
        p.DeleteAllRenderJobs()
        # звук рендерится пресетом: формата wav и кодека aac в списке Resolve нет
        if not p.LoadRenderPreset("Audio Only"):
            p.SetCurrentRenderFormatAndCodec("mov", "ProRes422P")
        p.SetRenderSettings({"SelectAllFrames": True, "TargetDir": str(qa_dir),
                             "CustomName": stem, "ExportVideo": False, "ExportAudio": True})
        job = p.AddRenderJob()
        t0 = time.time()
        p.StartRendering([job])
        # IsRenderingInProgress() успевает вернуть False ДО фактического старта — ждём по статусу задания
        while True:
            st = (p.GetRenderJobStatus(job) or {}).get("JobStatus", "")
            if st in ("Complete", "Failed", "Cancelled"):
                break
            if time.time() - t0 > dur + 300:        # с запасом: час звука рендерится ~90 с
                break
            time.sleep(2)
        for _ in range(15):                          # файл появляется чуть позже статуса
            if found():
                break
            time.sleep(2)
        if st != "Complete":
            print(f"рендер звука: статус «{st or 'нет'}»")
        got = found()
        wav = got[-1] if got else wav
        print(f"рендер {time.time() - t0:.0f} с → {wav.name} "
              f"({wav.stat().st_size / 1e6:.0f} МБ)" if got else "рендер не дал файла")

    if wav.exists():
        x, e = env_of(wav)
        floor = float(np.percentile(e, 20))
        speech = floor + 12
        joints = []
        pos = 0.0
        for k in edl.get("keep", [])[:-1]:
            pos += k[1] - k[0]
            joints.append(pos)
        for t in joints:
            i = int(t / 0.01)
            before, after = e[max(i - 12, 0):i], e[i:i + 12]
            cut_word = (before.size and before.max() > speech) and (after.size and after.max() > speech) \
                and (min(before.min(), after.min()) > speech)
            s = int(t * SR)
            jump = float(np.max(np.abs(np.diff(x[max(s - 4, 0):s + 4])))) if x.size > s + 4 else 0.0
            quiet = float(min(before.min() if before.size else 0, after.min() if after.size else 0))
            if cut_word:
                bad("стыки", f"склейка {fmt_t(t)}", f"речь не прерывается — возможен обрыв слова", t)
            elif jump > 0.12:
                bad("стыки", f"склейка {fmt_t(t)}", f"скачок сэмплов {jump:.2f} — возможен щелчок", t)
            else:
                ok("стыки", f"склейка {fmt_t(t)}", f"тихо: {quiet:.0f} дБ при фоне {floor:.0f} дБ")
        ok("стыки", "источник проверки", f"{wav.name} ({x.size / SR / 60:.1f} мин)")
    else:
        bad("стыки", "звук таймлайна", "не отрендерился — стыки не проверены")

    # ---- жёлтые маркеры на каждое ⚠ ----
    used = set(marks)
    for w in warns:
        if w.get("t") is None:
            continue
        f = int(round(w["t"] * fps))
        while f in used:
            f += 1
        used.add(f)
        tl.AddMarker(f, "Yellow", f"QA: {w['what']}", w["detail"], 1, "qa")

    # ---- отчёт ----
    L = [f"# Проверка качества — {tl_name}", "", f"{now()[:16]} · длина {fmt_t(dur)} · {fps:g} fps", "",
         "| | уровень | что | детали |", "|---|---|---|---|"]
    L += [f"| {s} | {lv} | {w} | {d} |" for s, lv, w, d in rows]
    n_bad = sum(1 for s, *_ in rows if s == "⚠")
    L += ["", f"**Итог: {len(rows) - n_bad} OK, {n_bad} ⚠**" + ("" if not n_bad else " — каждое ⚠ помечено жёлтым маркером.")]
    (qa_dir / f"{slug or 'timeline'}.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    append_jsonl(db(root) / "qa.jsonl", {"ts": now(), "timeline": tl_name, "ok": len(rows) - n_bad, "warn": n_bad,
                                         "warns": warns})
    print("\n".join(L))
    seq = journal(root, "qa", timeline=tl_name, ok=len(rows) - n_bad, warn=n_bad)
    print(f"\n[seq {seq}] → 07_qa/")


if __name__ == "__main__":
    v = sys.argv
    if len(v) < 2:
        raise SystemExit(__doc__)
    g = lambda k, d=None: (v[v.index(k) + 1] if k in v else d)
    main(v[1], g("--timeline", "v01 · смысловой"), do_render="--no-render" not in v)
