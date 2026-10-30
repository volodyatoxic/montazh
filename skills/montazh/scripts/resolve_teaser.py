"""Вертикальный тизер или рилс из спеки: кадрирование 9:16, панч-зумы, оверлей, саунд-дизайн.

  python3 resolve_teaser.py <проект> --spec <spec.json> --assets <папка> [--name "..."] [--render]

Видео берётся из ISO-камер и кадрируется в вертикаль средствами Resolve (Pan/Tilt/ZoomX/ZoomY на
каждом клипе — ключевых кадров API не умеет, поэтому приближения делаются сменой плана, как в рилсах).
Звук — дорожки P8 со смещением из sync.json ПЛЮС поправка липсинка (lipsync_s).
Оверлей с титрами ложится на V2, саунд-дизайн — на отдельную дорожку. Исходный звук не трогается.
"""
import json, re, sys, time
from pathlib import Path
from common import agent, connect, db, ensure, fmt_t, journal, now, read_json, append_jsonl

# Resolve масштабирует исходник «вписать целиком»: кадр 1920x1080 на холсте 1080x1920
# занимает 1080x607.5. Отсюда зум, при котором по вертикали видно ровно h строк исходника.
FIT_W, FIT_H = 1080.0, 607.5


def zoom_for(h):
    return 1920.0 / (FIT_H * (h / 1080.0))


def framing(face_x, face_y, h, face_at, punch=1.0):
    z = zoom_for(h) * punch
    y_c = face_y + (0.5 - face_at) * h
    pan = -((face_x / 1920.0 * FIT_W) - FIT_W / 2) * z
    tilt = ((y_c / 1080.0 * FIT_H) - FIT_H / 2) * z
    return z, pan, tilt


def find_tl(p, name):
    for i in range(1, p.GetTimelineCount() + 1):
        if p.GetTimelineByIndex(i).GetName() == name:
            return p.GetTimelineByIndex(i)
    return None


def main(proj, spec_path, assets, name=None, do_render=False):
    root = Path(proj).resolve()
    ensure(root)
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    assets = Path(assets)
    sync = read_json(db(root) / "sync.json", {})
    a, b = sync.get("a", 0.0), sync.get("b", 0.0)
    fps = float(spec.get("fps", 50))
    lip = int(round(float(sync.get("lipsync_s", 0.0)) * fps))
    sp = read_json(db(root) / "speakers.json", {})
    mics = list((sp.get("tracks") or {}).keys())
    name = name or spec["name"]
    print(f"{name}: {len(spec['segments'])} фрагментов, {spec['duration']:.1f} с · "
          f"липсинк {lip:+d} кадра")

    r, pm, p = connect()
    if p.IsRenderingInProgress():
        raise SystemExit("Resolve рендерит — подожди")
    if find_tl(p, name):
        raise SystemExit(f"таймлайн «{name}» уже есть — не перезаписываю")
    mp = p.GetMediaPool()

    pool = {}
    def walk(f):
        for c in f.GetClipList():
            pool[c.GetName()] = c
        for s in f.GetSubFolderList():
            walk(s)
    walk(mp.GetRootFolder())
    cam_clip = {int(m.group(1)): c for n, c in pool.items()
                if n.lower().endswith((".mp4", ".mov")) and (m := re.search(r"CAM\s*(\d+)", n))}
    mic_clip = {m: pool.get(f"{m}.WAV") or pool.get(f"{m}.wav") for m in mics}

    # ассеты моушна — своей папкой в медиапуле
    rootf = mp.GetRootFolder()
    folder = next((f for f in rootf.GetSubFolderList() if f.GetName() == "motion"), None) \
        or mp.AddSubFolder(rootf, "motion")
    mp.SetCurrentFolder(folder)
    have = {Path(c.GetClipProperty("File Path") or "").name: c for c in folder.GetClipList()}
    asset = {}
    for fn in ("overlay.mov", "endcard.mov", "sfx.wav"):
        c = have.get(fn)
        if c is None:
            got = mp.ImportMedia([str(assets / fn)])
            c = got[0] if got else None
        if c is None:
            raise SystemExit(f"не импортировался {assets / fn}")
        asset[fn] = c
    mp.SetCurrentFolder(rootf)

    tl = mp.CreateEmptyTimeline(name)
    if not tl:
        raise SystemExit("CreateEmptyTimeline не сработал")
    p.SetCurrentTimeline(tl)
    tl.SetSetting("useCustomSettings", "1")
    tl.SetSetting("timelineResolutionWidth", str(spec.get("w", 1080)))
    tl.SetSetting("timelineResolutionHeight", str(spec.get("h", 1920)))
    tl.SetSetting("timelineFrameRate", str(fps))
    while tl.GetTrackCount("video") < 2:
        tl.AddTrack("video")
    while tl.GetTrackCount("audio") < len(mics) + 1:
        tl.AddTrack("audio", "mono")
    start = tl.GetStartFrame()
    F = lambda t: int(round(t * fps))

    def push(batch):
        for i in range(0, len(batch), 40):
            if not mp.AppendToTimeline(batch[i:i + 40]):
                raise SystemExit(f"AppendToTimeline вернул пусто на порции {i}")

    # V1: фрагменты камер + концовка
    vb = []
    for s in spec["segments"]:
        vb.append({"mediaPoolItem": cam_clip[s["cam"]], "startFrame": F(s["in"]), "endFrame": F(s["out"]),
                   "recordFrame": start + F(s["new"]), "trackIndex": 1, "mediaType": 1})
    ec = spec["endcard"]
    vb.append({"mediaPoolItem": asset["endcard.mov"], "startFrame": 0, "endFrame": F(ec["dur"]),
               "recordFrame": start + F(ec["t"]), "trackIndex": 1, "mediaType": 1})
    push(vb)

    # V2: оверлей с титрами
    push([{"mediaPoolItem": asset["overlay.mov"], "startFrame": 0, "endFrame": F(spec["duration"]),
           "recordFrame": start, "trackIndex": 2, "mediaType": 1}])

    # звук: мики по фрагментам + саунд-дизайн
    for ti, m in enumerate(mics, start=1):
        ab = []
        for s in spec["segments"]:
            src = F(s["in"] + a + b * s["in"]) - lip
            ab.append({"mediaPoolItem": mic_clip[m], "startFrame": src,
                       "endFrame": src + (F(s["out"]) - F(s["in"])),
                       "recordFrame": start + F(s["new"]), "trackIndex": ti, "mediaType": 2})
        push(ab)
        tl.SetTrackName("audio", ti, m)
    sfx_ti = len(mics) + 1
    push([{"mediaPoolItem": asset["sfx.wav"], "startFrame": 0, "endFrame": F(spec["duration"]),
           "recordFrame": start, "trackIndex": sfx_ti, "mediaType": 2}])
    tl.SetTrackName("audio", sfx_ti, "SFX")

    # кадрирование 9:16 и панч-зумы
    items = tl.GetItemListInTrack("video", 1) or []
    fr = spec["framing"]
    h, at = spec.get("visible_height", 1040), spec.get("face_at", 0.36)
    done = 0
    for it in items:
        rec = (it.GetStart() - start) / fps
        s = min((x for x in spec["segments"] if abs(x["new"] - rec) < 0.05),
                key=lambda x: abs(x["new"] - rec), default=None)
        if not s:
            continue
        f = fr[str(s["cam"])]
        z, pan, tilt = framing(f["face_x"], f["face_y"], h, at, s.get("punch", 1.0))
        it.SetProperty("ZoomGang", True)
        for k, v in (("ZoomX", z), ("ZoomY", z), ("Pan", pan), ("Tilt", tilt)):
            it.SetProperty(k, v)
        done += 1
    print(f"кадрирование задано на {done} клипах · зум базовый {zoom_for(h):.3f}")

    append_jsonl(db(root) / "timelines.jsonl",
                 {"ts": now(), "project": p.GetName(), "timeline": name, "kind": "вертикальный тизер",
                  "duration": spec["duration"], "segments": len(spec["segments"]),
                  "lipsync_frames": lip, "created_by": "resolve_teaser.py"})
    seq = journal(root, "teaser", timeline=name, segments=len(spec["segments"]), lipsync=lip)
    print(f"[seq {seq}] таймлайн «{name}» собран, длина {fmt_t((tl.GetEndFrame() - start) / fps)}")

    if do_render:
        out = root / "renders"
        out.mkdir(exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_",
                      name.encode("ascii", "ignore").decode()).strip("_") or "teaser"
        code = re.match(r"\d{6}", root.name)               # код съёмки ГГММДД из имени папки
        stem = f"{code.group()}_{stem}" if code else stem
        r.OpenPage("deliver")
        p.DeleteAllRenderJobs()
        p.LoadRenderPreset("H.264 Master")
        p.SetRenderSettings({"SelectAllFrames": True, "TargetDir": str(out), "CustomName": stem,
                             "ExportVideo": True, "ExportAudio": True,
                             "FormatWidth": spec.get("w", 1080), "FormatHeight": spec.get("h", 1920)})
        job = p.AddRenderJob()
        p.StartRendering([job])
        t0 = time.time()
        while True:
            st = (p.GetRenderJobStatus(job) or {}).get("JobStatus", "")
            if st in ("Complete", "Failed", "Cancelled") or time.time() - t0 > 900:
                break
            time.sleep(2)
        print(f"рендер: {st} за {time.time() - t0:.0f} с → {out}/{stem}.mp4")


if __name__ == "__main__":
    v = sys.argv
    if len(v) < 2:
        raise SystemExit(__doc__)
    g = lambda k, d=None: (v[v.index(k) + 1] if k in v else d)
    main(v[1], g("--spec"), g("--assets"), g("--name"), "--render" in v)
