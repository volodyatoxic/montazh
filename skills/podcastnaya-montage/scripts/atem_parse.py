"""Разбор лога ATEM (.drp): нарезка оператора, короткие склейки, метки PiP.

  python3 atem_parse.py <проект> [--drp <путь>] [--dur <секунды>]

Формат (проверено на ATEM Mini Pro ISO, Podcast.drp от 29.10.2026): JSON-строки.
Первая — заголовок: videoMode, masterTimecode (старт записи), sources[], стартовое состояние
mixEffectBlocks[]. Дальше по строке на изменение: masterTimecode + только изменившиеся поля.

Пишет: 00_db/atem_cuts.jsonl (планы оператора), 00_db/atem.json (сводка), тревоги, журнал.
Исходники только читает.
"""
import json, re, sys
from pathlib import Path
from common import alert, db, ensure, fmt_t, journal, now, read_json, read_jsonl, write_json

# пороги из references/cutting.md
FLASH = 0.5          # план короче — почти наверняка не та кнопка
PARASITE = 1.5       # 0.5–1.5 с — проверить, говорит ли человек в кадре
STROBE_N, STROBE_WIN = 3, 4.0   # ≥3 переключений за 4 с


def fps_of(video_mode):
    m = re.search(r"(\d+(?:\.\d+)?)[ip]?$", (video_mode or "").strip())
    return float(m.group(1)) if m else 25.0


def tc_frames(tc, fps):
    h, m, s, f = (int(x) for x in tc.split(":"))
    return ((h * 60 + m) * 60 + s) * fps + f


def merge_me(state, patch):
    """Событие несёт только изменившиеся поля ME-блока — доливаем в состояние."""
    for blk in patch:
        cur = state.setdefault(blk.get("_index_", 0), {})
        for k, v in blk.items():
            if k == "upstreamKeys":
                keys = cur.setdefault("upstreamKeys", {})
                for uk in v:
                    keys.setdefault(uk.get("_index_", 0), {}).update(uk)
            else:
                cur[k] = v
    return state


def pip_on(me):
    return any(k.get("onAir") for k in me.get("upstreamKeys", {}).values())


def main(proj, drp_path=None, dur=None):
    root = Path(proj).resolve()
    ensure(root)

    if drp_path:
        drp = Path(drp_path)
    else:
        cand = [r["path"] for r in read_jsonl(db(root) / "files.jsonl") if r.get("role") == "atem_project"]
        if not cand:
            raise SystemExit("нет .drp в files.jsonl — сначала scan.py")
        drp = root / cand[0]
    lines = [json.loads(l) for l in drp.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    head, events = lines[0], lines[1:]
    fps = fps_of(head.get("videoMode"))
    t0 = tc_frames(head["masterTimecode"], fps)
    src_name = {s["_index_"]: (s.get("name") or Path(s.get("file", "")).name or s.get("type")) for s in head["sources"]}
    cam_of = {s["_index_"]: int(re.search(r"CAM\s*(\d+)", s.get("file", "")).group(1))
              for s in head["sources"] if s.get("type") == "Video" and re.search(r"CAM\s*(\d+)", s.get("file", ""))}

    if dur is None:
        durs = [r.get("duration") for r in read_jsonl(db(root) / "files.jsonl")
                if r.get("role") in ("atem_iso", "atem_program") and r.get("duration")]
        dur = max(durs) if durs else None

    state = merge_me({}, head["mixEffectBlocks"])
    me = state[0]
    shots, pips, t_prev, src_prev, pip_prev = [], [], 0.0, me.get("source"), pip_on(me)
    for e in events:
        t = (tc_frames(e["masterTimecode"], fps) - t0) / fps
        state = merge_me(state, e.get("mixEffectBlocks", []))
        me = state[0]
        s_now, pip_now = me.get("source"), pip_on(me)
        if s_now != src_prev:
            shots.append({"t": round(t_prev, 3), "dur": round(t - t_prev, 3), "source": src_prev,
                          "cam": cam_of.get(src_prev), "name": src_name.get(src_prev)})
            t_prev, src_prev = t, s_now
        if pip_now != pip_prev:
            pips.append({"t": round(t, 3), "on": pip_now})
            pip_prev = pip_now
    end = dur if dur else t_prev
    shots.append({"t": round(t_prev, 3), "dur": round(end - t_prev, 3), "source": src_prev,
                  "cam": cam_of.get(src_prev), "name": src_name.get(src_prev)})

    # метки оператора: от включения PiP до выключения
    marks = []
    for i, p in enumerate(pips):
        if p["on"]:
            off = next((q["t"] for q in pips[i + 1:] if not q["on"]), end)
            marks.append({"t": p["t"], "dur": round(off - p["t"], 3)})

    # короткие склейки и строб
    for i, sh in enumerate(shots):
        if sh["dur"] < FLASH:
            sh["flag"] = "вспышка"
        elif sh["dur"] < PARASITE:
            sh["flag"] = "короткая"
    for i in range(len(shots)):
        w = [s for s in shots[i:] if s["t"] < shots[i]["t"] + STROBE_WIN]
        if len(w) >= STROBE_N + 1:
            for s in w:
                s["flag"] = "строб"

    run = now()
    with open(db(root) / "atem_cuts.jsonl", "w", encoding="utf-8") as f:
        for i, sh in enumerate(shots):
            f.write(json.dumps({"i": i, **sh}, ensure_ascii=False) + "\n")

    by_cam = {}
    for sh in shots:
        by_cam.setdefault(sh["cam"] or sh["name"], [0, 0.0])
        by_cam[sh["cam"] or sh["name"]][0] += 1
        by_cam[sh["cam"] or sh["name"]][1] += sh["dur"]
    flags = [s for s in shots if s.get("flag")]
    summary = {"run": run, "drp": str(drp.relative_to(root)) if drp.is_relative_to(root) else str(drp),
               "video_mode": head.get("videoMode"), "fps": fps, "master_timecode": head["masterTimecode"],
               "recording_id": head.get("recordingId"), "duration": end, "shots": len(shots),
               "by_cam": {str(k): {"shots": v[0], "sec": round(v[1], 1)} for k, v in by_cam.items()},
               "flags": flags, "operator_marks": marks}
    write_json(db(root) / "atem.json", summary)

    for s in flags:
        if s.get("flag") == "вспышка":
            alert(root, "picture", f"Вспышка: план {s['dur']:.2f} с на CAM {s['cam']} — вероятно не та кнопка",
                  "Podcast.drp", t=s["t"], run=run)
        elif s.get("flag") == "строб":
            alert(root, "picture", f"Строб: частые переключения около {fmt_t(s['t'])}", "Podcast.drp", t=s["t"], run=run)
    for m in marks:
        alert(root, "picture", f"Метка оператора (PiP) {fmt_t(m['t'])} + {m['dur']:.1f} с — смотреть материал до метки",
              "Podcast.drp", t=m["t"], run=run)

    print(f"ATEM {head.get('videoMode')} · старт {head['masterTimecode']} · запись {fmt_t(end)} · планов {len(shots)}")
    print("\n| камера | планов | экранное время | доля |\n|---|---|---|---|")
    for k, v in sorted(by_cam.items(), key=lambda kv: -kv[1][1]):
        print(f"| {k} | {v[0]} | {fmt_t(v[1])} | {100 * v[1] / end:.0f}% |")
    lens = sorted(s["dur"] for s in shots)
    print(f"\nПлан: медиана {lens[len(lens) // 2]:.1f} с · самый короткий {lens[0]:.2f} с · самый длинный {lens[-1]:.0f} с")
    if flags:
        print(f"\nПодозрительные планы ({len(flags)}):")
        for s in flags:
            print(f"  {fmt_t(s['t'])}  {s['dur']:.2f} с  CAM {s['cam']}  — {s['flag']}")
    print(f"\nМетки оператора (PiP): {len(marks)}" + (":" if marks else ""))
    for m in marks:
        print(f"  {fmt_t(m['t'])} + {m['dur']:.1f} с")
    seq = journal(root, "atem_parse", shots=len(shots), flags=len(flags), marks=len(marks), fps=fps)
    print(f"\n[seq {seq}] → _agent/00_db/atem_cuts.jsonl, atem.json")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    a = sys.argv
    main(a[1], drp_path=(a[a.index("--drp") + 1] if "--drp" in a else None),
         dur=(float(a[a.index("--dur") + 1]) if "--dur" in a else None))
