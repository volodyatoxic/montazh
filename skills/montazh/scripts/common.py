"""Общее для скриптов montazh: папка _agent, база, журнал, ffprobe, подключение к Resolve.

CLI:  python3 common.py log <проект> "что сделал"     — ручная запись в журнал
"""
import json, os, platform, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
VERSION = (SKILL / "VERSION").read_text().strip() if (SKILL / "VERSION").exists() else "dev"

STAGES = ["00_db", "00_db/transcript", "00_db/snapshots", "01_intake", "02_sync", "03_transcript",
          "04_edit", "05_gfx", "06_reels", "07_qa", "cache"]
SKIP_DIRS = {"_agent", "renders", ".git", "__MACOSX"}


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def agent(proj):
    return Path(proj).resolve() / "_agent"


def db(proj):
    return agent(proj) / "00_db"


def ensure(proj):
    a = agent(proj)
    for s in STAGES:
        (a / s).mkdir(parents=True, exist_ok=True)
    return a


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def append_jsonl(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def journal(proj, action, **data):
    """Запись в журнал со сквозным номером. Возвращает seq."""
    j = db(proj) / "journal.jsonl"
    seq = len(read_jsonl(j)) + 1
    append_jsonl(j, {"seq": seq, "ts": now(), "action": action, **data})
    return seq


def alert(proj, kind, text, where=None, t=None, run=None):
    append_jsonl(db(proj) / "alerts.jsonl",
                 {"ts": now(), "kind": kind, "text": text, "where": where, "t": t, "run": run})


def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout or "{}")


def fmt_t(sec):
    sec = float(sec or 0)
    h, m, s = int(sec // 3600), int(sec % 3600 // 60), sec % 60
    return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m}:{s:04.1f}"


# ---------- Resolve ----------
def _resolve_paths():
    sysname = platform.system()
    if sysname == "Darwin":
        api = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
        lib = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
    elif sysname == "Windows":
        api = os.path.expandvars(r"%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting")
        lib = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
    else:
        api = "/opt/resolve/Developer/Scripting"
        lib = "/opt/resolve/libs/Fusion/fusionscript.so"
    return os.environ.get("RESOLVE_SCRIPT_API", api), os.environ.get("RESOLVE_SCRIPT_LIB", lib)


def connect(wait=60):
    """→ (resolve, project_manager, project). Ждёт до wait секунд, пока Resolve ответит."""
    api, lib = _resolve_paths()
    os.environ["RESOLVE_SCRIPT_API"], os.environ["RESOLVE_SCRIPT_LIB"] = api, lib
    mod = os.path.join(api, "Modules")
    if mod not in sys.path:
        sys.path.append(mod)
    import DaVinciResolveScript as dvr
    t0 = time.time()
    while True:
        r = dvr.scriptapp("Resolve")
        if r and r.GetProjectManager() and r.GetVersionString():
            pm = r.GetProjectManager()
            return r, pm, pm.GetCurrentProject()
        if time.time() - t0 > wait:
            raise SystemExit("Resolve не отвечает: не запущен, висит диалог (обновление?) или выключен External scripting = Local")
        time.sleep(3)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "log":
        ensure(sys.argv[2])
        print("seq", journal(sys.argv[2], "manual", text=" ".join(sys.argv[3:])))
    else:
        print(__doc__)
