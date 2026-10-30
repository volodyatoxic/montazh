"""v01 — сборка версии по плану резов: вырезы, разбавление длинных планов, маркеры.

  python3 resolve_v01.py <проект> [--plan <путь>] [--name "v01 · смысловой"] [--dry]

План — 04_edit/<версия>/plan.json: резы (t, end, reason, note), сомнения, главы, настройки разбавления.
Видео собирается из нарезки оператора (00_db/atem_cuts.jsonl) по оставшимся кускам, звук — дорожками
P8 с посчитанной синхрой (00_db/sync.json), смещение считается для каждого куска отдельно, поэтому
дрейф часов не накапливается.

API Resolve не умеет резать клип, поэтому версия собирается заново из точных кадров исходников,
а не дубликатом с вырезами. Старые таймлайны не трогаются.

Маркеры: красный — место выреза, фиолетовый — глава, жёлтый — сомнение.
Пишет 00_db/timelines.jsonl, 04_edit/<версия>/edl.json, журнал.
"""
import json, sys
from pathlib import Path
import numpy as np
from common import agent, connect, db, ensure, fmt_t, journal, now, read_json, read_jsonl, append_jsonl, write_json

HOP = 0.01


def load_env(root, names):
    out = {}
    for n in names:
        p = agent(root) / "cache" / f"env_{n}.npy"
        if p.exists():
            out[n] = np.load(p)
    return out


def silence_check(envs, t, win=0.12, margin=12.0):
    """Все микрофоны тише своего фона +margin дБ на окне win вокруг t?"""
    if not envs:
        return None
    for e in envs.values():
        floor = np.percentile(e, 20)
        i, j = int((t - win / 2) / HOP), int((t + win / 2) / HOP)
        w = e[max(i, 0):max(j, 1)]
        if w.size == 0 or w.max() > floor + margin:
            return False
    return True


def speaker_at(envs, t, dur=2.0):
    best, lvl = None, -1e9
    for n, e in envs.items():
        i, j = int(t / HOP), int((t + dur) / HOP)
        w = e[max(i, 0):max(j, 1)]
        if w.size and float(np.percentile(w, 80)) > lvl:
            lvl, best = float(np.percentile(w, 80)), n
    return best


def snap_silence(envs, t, win=0.35, need=0.12):
    """Ближайшая к t самая тихая точка в ±win — туда и режем.

    Окно нарочно узкое: шире — и точка реза перескочит в соседнюю фразу и срежет слово.
    Тишины рядом нет — возвращаем как было, выше это станет жёлтым маркером.
    """
    if not envs:
        return t
    n = int(need / HOP)
    i0, i1 = int((t - win) / HOP), int((t + win) / HOP)
    i0 = max(i0, 0)
    stack = np.stack([e[i0:i1] for e in envs.values()]) if i1 > i0 else None
    if stack is None or stack.shape[1] < n + 2:
        return t
    loud = stack.max(axis=0)
    roll = np.convolve(loud, np.ones(n) / n, mode="valid")
    centre = np.arange(roll.size) + n / 2
    penalty = np.abs(centre - (t / HOP - i0)) * 0.02      # при равной тишине ближе к задуманному
    return (i0 + centre[int(np.argmin(roll + penalty))]) * HOP


def reaction_point(envs, cam_of_mic, cam, lo, hi):
    """Момент в [lo, hi], где слушатель громче всего — туда ставим реакцию."""
    other = [m for m, c in cam_of_mic.items() if c != cam]
    if not other or not envs:
        return (lo + hi) / 2
    e = envs.get(other[0])
    if e is None:
        return (lo + hi) / 2
    i0, i1 = int(lo / HOP), int(hi / HOP)
    w = e[max(i0, 0):max(i1, i0 + 1)]
    if w.size < 50:
        return (lo + hi) / 2
    roll = np.convolve(w, np.ones(50) / 50, mode="valid")
    return lo + (int(np.argmax(roll)) + 25) * HOP


def subtract(total, cuts):
    """[0, total] минус отрезки резов → список оставшихся кусков."""
    keep, pos = [], 0.0
    for c in sorted(cuts, key=lambda x: x["t"]):
        if c["t"] > pos:
            keep.append((pos, min(c["t"], total)))
        pos = max(pos, c["end"])
    if pos < total:
        keep.append((pos, total))
    return [(a, b) for a, b in keep if b - a > 0.04]


def main(proj, plan_path=None, name=None, dry=False):
    root = Path(proj).resolve()
    ensure(root)
    plan = read_json(plan_path or agent(root) / "04_edit" / "v01" / "plan.json")
    if not plan:
        raise SystemExit("нет плана 04_edit/v01/plan.json")
    sync = read_json(db(root) / "sync.json")
    shots = read_jsonl(db(root) / "atem_cuts.jsonl")
    sp = read_json(db(root) / "speakers.json", {})
    people = sp.get("people", {})
    cam_of_mic = {m: d.get("cam") for m, d in people.items() if d.get("cam")}
    wide = sp.get("wide_cam") or 1
    name = name or f"{plan['version']} · {plan['level']}"
    fps = float(plan.get("fps") or 50)
    total = float(plan["source_duration"])
    a, b = sync["a"], sync["b"]
    envs = load_env(root, list(cam_of_mic))

    keep = subtract(total, plan["cuts"])
    new_total = sum(y - x for x, y in keep)
    print(f"{name}: оставляем {len(keep)} кусков, {fmt_t(new_total)} из {fmt_t(total)} "
          f"(вырезано {fmt_t(total - new_total)})")

    # каждый рез подводим к самой тихой точке рядом; где тишины нет — жёлтый маркер
    for c in plan["cuts"]:
        for edge in ("t", "end"):
            t = c[edge]
            if t <= 0.05 or t >= total - 0.05:
                continue
            snapped = round(snap_silence(envs, t), 3)
            if abs(snapped - t) > 0.004:
                print(f"  рез {fmt_t(t)} ({edge}) → {fmt_t(snapped)} (сдвиг {(snapped - t) * 1000:+.0f} мс)")
                c[edge] = snapped
            if silence_check(envs, c[edge]) is False:
                print(f"  ⚠ {fmt_t(c[edge])} ({edge}) — полной тишины нет, режу по границе слова")
                c.setdefault("warn", []).append(edge)
    keep = subtract(total, plan["cuts"])
    new_total = sum(y - x for x, y in keep)

    # ---- видео: куски нарезки оператора внутри оставшегося ----
    def shots_in(t0, t1):
        out = []
        for sh in shots:
            s0, s1 = sh["t"], sh["t"] + sh["dur"]
            x, y = max(s0, t0), min(s1, t1)
            if y - x > 0.04 and sh.get("cam"):
                out.append({"cam": sh["cam"], "t": x, "end": y})
        return out

    pieces, new_t, joins = [], 0.0, []
    for k, (t0, t1) in enumerate(keep):
        part = shots_in(t0, t1)
        if k:
            joins.append((new_t, len(pieces)))
        for pc in part:
            pc["new_t"] = new_t + (pc["t"] - t0)
            pc["k"] = k
            pieces.append(pc)
        new_t += t1 - t0

    # склейка после выреза: если ракурс тот же — прячем сменой плана
    cut_cfg = plan.get("cutaway", {})
    INS = float(cut_cfg.get("insert", 4.0))
    MAX = float(cut_cfg.get("max_shot", 30.0))
    WIDE_EVERY = int(cut_cfg.get("wide_every", 3))

    def alt_cam(cam, t, n):
        if cam == wide:
            mic = speaker_at(envs, t)
            return cam_of_mic.get(mic, 2)
        if n % WIDE_EVERY == 0:
            return wide
        return next((c for m, c in cam_of_mic.items() if c != cam), wide)

    hidden = 0
    for new_pos, idx in joins:
        if idx == 0 or idx >= len(pieces):
            continue
        before, after = pieces[idx - 1], pieces[idx]
        if before["cam"] != after["cam"]:
            continue
        span = min(INS, after["end"] - after["t"] - 0.5)
        if span < 1.5:
            continue
        cut_piece = {"cam": alt_cam(after["cam"], after["t"], hidden), "t": after["t"],
                     "end": after["t"] + span, "new_t": after["new_t"], "k": after["k"],
                     "why": "прячу джамп-кат"}
        after["t"] += span
        after["new_t"] += span
        pieces.insert(idx, cut_piece)
        hidden += 1

    # разбавление длинных планов
    out, n_ins = [], 0
    LO = max(MAX * 0.6, 12.0)          # раньше LO план не меняем, иначе дробим без нужды
    for pc in pieces:
        if pc["end"] - pc["t"] <= MAX:
            out.append(pc)
            continue
        pos, nt = pc["t"], pc["new_t"]
        while pc["end"] - pos > MAX:
            hi = min(pos + MAX, pc["end"] - 6.0)
            if hi <= pos + LO:
                break
            at = reaction_point(envs, cam_of_mic, pc["cam"], pos + LO, hi)
            ins = INS if n_ins % 2 else max(INS - 1.2, 2.5)
            if at - pos < 6.0 or pc["end"] - (at + ins) < 6.0:
                break
            out.append({"cam": pc["cam"], "t": pos, "end": at, "new_t": nt, "k": pc["k"]})
            nt += at - pos
            pos = at
            out.append({"cam": alt_cam(pc["cam"], pos, n_ins), "t": pos, "end": pos + ins, "k": pc["k"],
                        "new_t": nt, "why": "реакция слушателя — разбавляю длинный план"})
            pos, nt, n_ins = pos + ins, nt + ins, n_ins + 1
        out.append({"cam": pc["cam"], "t": pos, "end": pc["end"], "new_t": nt, "k": pc["k"]})
    pieces = out
    print(f"планов на V1: {len(pieces)} (было у оператора {len(shots)}) · "
          f"джамп-катов спрятано {hidden} · вставок в длинноты {n_ins}")

    # ---- кадровая раскладка ----
    # Всё считаем в кадрах и складываем нарастающим итогом: если позиции считать в секундах и
    # округлять каждую отдельно, на стыках появляются дыры в кадр, а звук уезжает от картинки.
    rec, vplan, rng = 0, [], {}
    for pc in pieces:
        s0, e0 = int(round(pc["t"] * fps)), int(round(pc["end"] * fps))
        if e0 <= s0:
            e0 = s0 + 1
        vplan.append({"cam": pc["cam"], "s": s0, "e": e0, "rec": rec, "k": pc["k"],
                      "t": pc["t"], "end": pc["end"], "why": pc.get("why")})
        rr = rng.setdefault(pc["k"], {"rec": rec, "len": 0})
        rr["len"] += e0 - s0
        rec += e0 - s0
    total_frames = rec

    # звук: на каждый оставшийся кусок свой сдвиг, посчитанный по его середине —
    # так ошибка хода часов делится пополам, а не копится к концу куска
    aplan = []
    for mic in cam_of_mic:
        for k, (t0, t1) in enumerate(keep):
            if k not in rng:
                continue
            mid = (t0 + t1) / 2
            s0 = int(round((t0 + a + b * mid) * fps))
            aplan.append({"mic": mic, "s": s0, "e": s0 + rng[k]["len"], "rec": rng[k]["rec"],
                          "t": t0, "end": t1, "offset_s": round(a + b * mid, 4)})

    def to_frame(t):
        for k, (t0, t1) in enumerate(keep):
            if k not in rng:
                continue
            if t0 <= t <= t1:
                return rng[k]["rec"] + int(round((t - t0) * fps))
            if t < t0:
                return rng[k]["rec"]
        return max(total_frames - 1, 0)

    edl = {"ts": now(), "timeline": name, "keep": keep, "video": vplan, "audio": aplan,
           "cuts": plan["cuts"], "new_duration": total_frames / fps, "frames": total_frames, "fps": fps}
    write_json(agent(root) / "04_edit" / plan["version"] / "edl.json", edl)
    if dry:
        print("--dry: в Resolve ничего не делаю")
        return

    # ---- сборка в Resolve ----
    r, pm, p = connect()
    if p.IsRenderingInProgress():
        raise SystemExit("Resolve рендерит — подожди")
    for i in range(1, p.GetTimelineCount() + 1):
        if p.GetTimelineByIndex(i).GetName() == name:
            raise SystemExit(f"таймлайн «{name}» уже есть — не перезаписываю")
    mp = p.GetMediaPool()

    pool = {}
    def find_clip(folder):
        for c in folder.GetClipList():
            pool[c.GetName()] = c
        for f in folder.GetSubFolderList():
            find_clip(f)
    find_clip(mp.GetRootFolder())
    cam_clip = {}
    for nm, c in pool.items():
        import re
        m = re.search(r"CAM\s*(\d+)", nm)
        if m and nm.lower().endswith((".mp4", ".mov")):
            cam_clip[int(m.group(1))] = c
    mic_clip = {m: pool.get(f"{m}.WAV") or pool.get(f"{m}.wav") for m in cam_of_mic}
    missing = [k for k, v in list(cam_clip.items()) + list(mic_clip.items()) if v is None]
    if any(v is None for v in mic_clip.values()):
        raise SystemExit(f"в медиапуле нет дорожек P8: {mic_clip}")

    tl = mp.CreateEmptyTimeline(name)
    if not tl:
        raise SystemExit("CreateEmptyTimeline не сработал")
    p.SetCurrentTimeline(tl)
    start = tl.GetStartFrame()
    while tl.GetTrackCount("audio") < len(cam_of_mic):
        tl.AddTrack("audio", "mono")

    def push(batch):
        for i in range(0, len(batch), 40):
            if not mp.AppendToTimeline(batch[i:i + 40]):
                raise SystemExit(f"AppendToTimeline вернул пусто на порции {i}")

    push([{"mediaPoolItem": cam_clip[x["cam"]], "startFrame": x["s"], "endFrame": x["e"],
           "recordFrame": start + x["rec"], "trackIndex": 1, "mediaType": 1} for x in vplan])
    for ti, mic in enumerate(cam_of_mic, start=1):
        push([{"mediaPoolItem": mic_clip[mic], "startFrame": x["s"], "endFrame": x["e"],
               "recordFrame": start + x["rec"], "trackIndex": ti, "mediaType": 2}
              for x in aplan if x["mic"] == mic])
        who = (people.get(mic) or {}).get("name")
        tl.SetTrackName("audio", ti, f"{mic} {who}" if who else mic)

    # ---- маркеры ----
    used = set()
    def mark(frame, color, nm, note, dur=1, data=""):
        f = max(int(frame), 0)
        while f in used:
            f += 1
        used.add(f)
        tl.AddMarker(f, color, nm, note, dur, data)

    placed = []
    for i, c in enumerate(sorted(plan["cuts"], key=lambda x: x["t"]), start=1):
        L = c["end"] - c["t"]
        at = to_frame(c["end"] if c["t"] <= 0.05 else c["t"])
        if at >= total_frames - 2:
            continue
        warn = " · рез не в тишине" if c.get("warn") else ""
        mark(at, "Red", f"вырез {i}: {c['reason']}",
             f"{fmt_t(c['t'])}–{fmt_t(c['end'])} ({L:.1f} с). {c['note']}{warn}", 1, f"cut{i}")
        placed.append({"i": i, "at_frame": at, **c})
        if c.get("warn"):
            mark(at, "Yellow", f"проверить стык {i}",
                 "Рез пришёлся не на полную тишину — послушай стык", 1)

    chapters = []
    for t, title in plan.get("chapters", []):
        nf = to_frame(t)
        if chapters and nf - chapters[-1][0] < 10 * fps:
            continue
        chapters.append((nf, title))
    if chapters:
        chapters[0] = (0, chapters[0][1])
    for nf, title in chapters:
        mark(nf, "Purple", title, "глава выпуска", 1, "chapter")

    for d in plan.get("doubts", []):
        mark(to_frame(d["t"]), "Yellow", "сомнение", d["text"], max(int(d.get("dur", 1) * fps), 1))
    mark(0, "Yellow", "проверить липсинк",
         "Синхра посчитана звук-к-звуку (+131 мс, +13,2 ppm, разброс 0,4 мс). "
         "Глазами по губам не проверялась — глянь 2–3 крупных плана.", 1)
    mark(0, "Cyan", "CAM 4 чёрная",
         "Вход CAM 4 писал чёрный кадр всю запись. В монтаже не используется, оператор её тоже не брал.", 1)

    rec = {"ts": now(), "project": p.GetName(), "timeline": name, "fps": fps, "frames": tl.GetEndFrame() - start,
           "duration": total_frames / fps, "video_clips": len(vplan), "cuts": len(plan["cuts"]),
           "chapters": len(chapters), "created_by": "resolve_v01.py"}
    append_jsonl(db(root) / "timelines.jsonl", rec)
    for c in placed:
        append_jsonl(db(root) / "decisions.jsonl",
                     {"ts": now(), "kind": "вырез", "version": plan["version"],
                      "text": f"{fmt_t(c['t'])}–{fmt_t(c['end'])} ({c['reason']})", "src": c["note"]})
    seq = journal(root, "v01", timeline=name, clips=len(pieces), cuts=len(plan["cuts"]), chapters=len(chapters))
    print(f"глав расставлено: {len(chapters)} · маркеров всего: {len(used)}")
    print(f"длина таймлайна: {fmt_t((tl.GetEndFrame() - start) / fps)} "
          f"(план {fmt_t(total_frames / fps)})")
    print(f"\n[seq {seq}] готов «{name}»")


if __name__ == "__main__":
    v = sys.argv
    if len(v) < 2:
        raise SystemExit(__doc__)
    g = lambda k, d=None: (v[v.index(k) + 1] if k in v else d)
    main(v[1], plan_path=g("--plan"), name=g("--name"), dry="--dry" in v)
