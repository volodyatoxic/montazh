"""Переложить дорожки микрофонов на таймлайне с поправкой липсинка.

  python3 resolve_resync.py <проект> --timeline "v01 · смысловой" [--new "v02 · липсинк"] [--shift <кадры>]

Корреляция равняет звук со звуком, а не с губами: встроенный звук ATEM опережает собственную
картинку, и дорожки P8, выставленные по нему, приходят раньше изображения. Поправка меряется
по хлопку (момент касания ладоней в кадре против атаки в мике) и живёт в 00_db/sync.json как
`lipsync_s`. Положительная поправка двигает звук ВПРАВО (позже).

Снимает клипы микрофонов с дорожек и кладёт их заново на те же позиции, сместив кадр
источника. Видео, маркеры таймлайна и всё остальное не трогает. С --new работает на дубликате.
"""
import sys
from pathlib import Path
from common import connect, db, ensure, fmt_t, journal, now, read_json, write_json, append_jsonl


def find_tl(p, name):
    for i in range(1, p.GetTimelineCount() + 1):
        if p.GetTimelineByIndex(i).GetName() == name:
            return p.GetTimelineByIndex(i)
    return None


def main(proj, tl_name, new_name=None, shift_frames=None, answer=None):
    root = Path(proj).resolve()
    ensure(root)
    sync = read_json(db(root) / "sync.json", {})
    mics = {m for m in (read_json(db(root) / "speakers.json", {}) or {}).get("tracks", {})}

    r, pm, p = connect()
    if p.IsRenderingInProgress():
        raise SystemExit("Resolve рендерит — подожди")
    tl = find_tl(p, tl_name)
    if tl is None:
        raise SystemExit(f"нет таймлайна «{tl_name}»")
    fps = float(tl.GetSetting("timelineFrameRate"))
    if shift_frames is None:
        shift_frames = int(round(float(sync.get("lipsync_s", 0.0)) * fps))
    if not shift_frames:
        raise SystemExit("поправка нулевая — нечего двигать (задай --shift или lipsync_s в sync.json)")

    if new_name:
        if find_tl(p, new_name):
            raise SystemExit(f"таймлайн «{new_name}» уже есть — не перезаписываю")
        p.SetCurrentTimeline(tl)
        tl = tl.DuplicateTimeline(new_name)
        if not tl:
            raise SystemExit("DuplicateTimeline не сработал")
        print(f"дубликат «{new_name}» ← «{tl_name}»")
    p.SetCurrentTimeline(tl)
    start = tl.GetStartFrame()

    # снимаем и запоминаем, что где стояло
    moved, items_to_del = [], []
    for ti in range(1, tl.GetTrackCount("audio") + 1):
        for it in tl.GetItemListInTrack("audio", ti) or []:
            mpi = it.GetMediaPoolItem()
            nm = (mpi.GetName() if mpi else it.GetName()) or ""
            if Path(nm).stem.upper() not in {m.upper() for m in mics}:
                continue
            moved.append({"track": ti, "mpi": mpi, "rec": it.GetStart(),
                          "src0": it.GetSourceStartFrame(), "len": it.GetDuration()})
            items_to_del.append(it)
    if not moved:
        raise SystemExit("на таймлайне нет клипов микрофонов")
    if not tl.DeleteClips(items_to_del, False):
        raise SystemExit("не удалось снять клипы микрофонов")
    print(f"снято клипов микрофонов: {len(moved)}")

    mp = p.GetMediaPool()
    batch = [{"mediaPoolItem": m["mpi"], "startFrame": m["src0"] - shift_frames,
              "endFrame": m["src0"] - shift_frames + m["len"], "recordFrame": m["rec"],
              "trackIndex": m["track"], "mediaType": 2} for m in moved]
    for i in range(0, len(batch), 40):
        if not mp.AppendToTimeline(batch[i: i + 40]):
            raise SystemExit(f"AppendToTimeline вернул пусто на порции {i}")
    print(f"положено заново со сдвигом {shift_frames:+d} кадра "
          f"({shift_frames / fps * 1000:+.0f} мс, звук {'позже' if shift_frames > 0 else 'раньше'})")

    if answer:
        used = set(tl.GetMarkers() or {})
        f = 0
        while f in used:
            f += 1
        tl.AddMarker(f, "Green", "липсинк поправлен", answer, 1, "resync")

    append_jsonl(db(root) / "timelines.jsonl",
                 {"ts": now(), "project": p.GetName(), "timeline": tl.GetName(), "from": tl_name,
                  "action": "переложены дорожки микрофонов", "shift_frames": shift_frames,
                  "created_by": "resolve_resync.py"})
    seq = journal(root, "resync", timeline=tl.GetName(), shift=shift_frames, clips=len(moved))
    print(f"[seq {seq}] готово: {tl.GetName()}")


if __name__ == "__main__":
    v = sys.argv
    if len(v) < 2:
        raise SystemExit(__doc__)
    g = lambda k, d=None: (v[v.index(k) + 1] if k in v else d)
    main(v[1], g("--timeline"), g("--new"),
         int(g("--shift")) if "--shift" in v else None, g("--answer"))
