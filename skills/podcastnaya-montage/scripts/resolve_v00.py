"""v00 — нулевая сборка: нарезка оператора ATEM + дорожки P8 с синхрой, звук ATEM выключен.

  python3 resolve_v00.py <проект> [--project <имя в Resolve>] [--timeline <имя исходного>]

Берёт таймлайн, импортированный из .drp (видео уже стоит), дублирует его в «v00 · нулевая сборка»,
докладывает каждую дорожку P8 отдельной аудиодорожкой со смещением из 00_db/sync.json и выключает
дорожку звука ATEM. Резов не делает, звук не обрабатывает, чужое не удаляет.

Пишет 00_db/timelines.jsonl и журнал.
"""
import sys
from pathlib import Path
from common import connect, db, ensure, fmt_t, journal, now, read_json, read_jsonl, append_jsonl

V00 = "v00 · нулевая сборка"
SRC = "ATEM · нарезка оператора"


def find_timeline(p, name):
    for i in range(1, p.GetTimelineCount() + 1):
        tl = p.GetTimelineByIndex(i)
        if tl.GetName() == name:
            return tl
    return None


def main(proj, project_name=None, src_name=None):
    root = Path(proj).resolve()
    ensure(root)
    sync = read_json(db(root) / "sync.json")
    if not sync or not sync.get("ok"):
        raise SystemExit("нет надёжной синхры в 00_db/sync.json — сначала sync.py")
    files = read_jsonl(db(root) / "files.jsonl")
    mics = [r for r in files if r.get("role") == "p8_track"]
    if not mics:
        raise SystemExit("нет дорожек P8 в files.jsonl")
    people = (read_json(db(root) / "speakers.json", {}) or {}).get("people", {})

    r, pm, p = connect()
    if p and p.IsRenderingInProgress():
        raise SystemExit("Resolve сейчас рендерит — подожди")
    if project_name and (not p or p.GetName() != project_name):
        if not pm.LoadProject(project_name):
            raise SystemExit(f"не открылся проект «{project_name}»")
        p = pm.GetCurrentProject()
    print(f"проект: {p.GetName()}")

    # исходный таймлайн ATEM
    src = find_timeline(p, src_name) if src_name else None
    if src is None:
        src = find_timeline(p, SRC)
    if src is None:
        cands = [p.GetTimelineByIndex(i) for i in range(1, p.GetTimelineCount() + 1)]
        cands = [t for t in cands if t.GetName() not in (V00,)]
        if not cands:
            raise SystemExit("не нашёл таймлайн ATEM")
        src = max(cands, key=lambda t: len(t.GetItemListInTrack("video", 1) or []))
        if src.GetName() != SRC:
            print(f"переименовываю «{src.GetName()}» → «{SRC}» (исходник оставляю нетронутым)")
            src.SetName(SRC)

    fps = float(src.GetSetting("timelineFrameRate"))
    start = src.GetStartFrame()
    tl_len = src.GetEndFrame() - start
    print(f"исходный таймлайн: {src.GetName()} · {fps} fps · {fmt_t(tl_len / fps)} · "
          f"клипов V1 {len(src.GetItemListInTrack('video', 1) or [])}")

    if find_timeline(p, V00):
        raise SystemExit(f"таймлайн «{V00}» уже есть — v00 не перезаписываю")
    p.SetCurrentTimeline(src)
    tl = src.DuplicateTimeline(V00)
    if not tl:
        raise SystemExit("DuplicateTimeline не сработал")
    p.SetCurrentTimeline(tl)
    print(f"создан «{V00}»")

    # дорожки P8 в медиапул
    mp = p.GetMediaPool()
    rootf = mp.GetRootFolder()
    folder = next((f for f in rootf.GetSubFolderList() if f.GetName() == "P8"), None) or mp.AddSubFolder(rootf, "P8")
    mp.SetCurrentFolder(folder)
    have = {c.GetClipProperty("File Path"): c for c in folder.GetClipList()}
    items = []
    for m in mics:
        path = str(root / m["path"])
        it = have.get(path)
        if it is None:
            got = mp.ImportMedia([path])
            it = got[0] if got else None
        if it is None:
            raise SystemExit(f"не импортировался {path}")
        items.append((m, it))
    mp.SetCurrentFolder(rootf)

    # смещение: P8_время = время_камеры + a + b·время_камеры, берём по середине записи
    off = sync["offset_at_middle"]
    off_fr = int(round(off * fps))
    print(f"смещение P8 по середине записи: {off * 1000:+.1f} мс = {off_fr:+d} кадра при {fps:g}p")

    added = []
    for n, (m, it) in enumerate(items):
        ti = tl.GetTrackCount("audio") + 1
        if not tl.AddTrack("audio", "mono"):
            raise SystemExit(f"не добавилась аудиодорожка {ti}")
        src_frames = int(float(it.GetClipProperty("Frames") or 0))
        if src_frames <= 0:
            src_frames = int(m["duration"] * fps)
        s0 = max(off_fr, 0)
        e0 = min(src_frames, s0 + tl_len)
        ok = mp.AppendToTimeline([{"mediaPoolItem": it, "startFrame": s0, "endFrame": e0,
                                   "recordFrame": start + max(-off_fr, 0), "trackIndex": ti, "mediaType": 2}])
        ch = m.get("ch") or Path(m["path"]).stem
        who = (people.get(ch) or {}).get("name")
        label = f"{ch} {who}" if who else ch
        tl.SetTrackName("audio", ti, label)
        added.append({"track": f"A{ti}", "name": label, "file": m["path"],
                      "start_frame": s0, "end_frame": e0, "frames": e0 - s0})
        print(f"  A{ti}  {label:<28} кадры {s0}–{e0} ({fmt_t((e0 - s0) / fps)})  {'ok' if ok else 'НЕ ВСТАЛ'}")

    # звук ATEM выключаем (не удаляем)
    tl.SetTrackEnable("audio", 1, False)
    tl.SetTrackName("audio", 1, "A1 ATEM (выключена)")
    print("A1 (звук ATEM) выключена, клипы на месте")

    rec = {"ts": now(), "project": p.GetName(), "timeline": V00, "from": src.GetName(),
           "fps": fps, "start_frame": start, "frames": tl_len, "sync_offset_s": off,
           "sync_offset_frames": off_fr, "audio_added": added, "created_by": "resolve_v00.py"}
    append_jsonl(db(root) / "timelines.jsonl", rec)
    append_jsonl(db(root) / "timelines.jsonl",
                 {"ts": now(), "project": p.GetName(), "timeline": src.GetName(),
                  "note": "импорт .drp — нарезка оператора, не трогать", "created_by": "ImportProject"})
    seq = journal(root, "v00", timeline=V00, tracks=len(added), offset_frames=off_fr)
    print(f"\n[seq {seq}] v00 готов. Дальше: проверить липсинк глазами на 2–3 крупных планах.")


if __name__ == "__main__":
    v = sys.argv
    if len(v) < 2:
        raise SystemExit(__doc__)
    g = lambda k, d=None: (v[v.index(k) + 1] if k in v else d)
    main(v[1], project_name=g("--project"), src_name=g("--timeline"))
