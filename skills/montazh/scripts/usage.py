"""Учёт времени и токенов по журналу сессии Claude Code.

  python3 usage.py <проект> mark <метка>        — отметка начала этапа (пишется в _agent/00_db/usage.jsonl)
  python3 usage.py <проект> report [<метка>]    — расход с отметки (по умолчанию с последней) до сейчас

Журнал сессии — самый свежий *.jsonl в ~/.claude/projects (плюс файлы сабагентов этой сессии).
Вызовы модели считаются по уникальному message.id (одна реплика бывает разбита на несколько строк).
Кэш не показываем — только время, вызовы, токены ответа и новые входные токены.
"""
import json, re, sys
from datetime import datetime
from pathlib import Path
from common import append_jsonl, db, ensure, now, read_jsonl

PROJECTS = Path.home() / ".claude" / "projects"


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def session_files(proj):
    """Журналы Claude Code лежат в ~/.claude/projects/<путь папки, где всё кроме букв и цифр → «-»>.
    Берём самый свежий из папки этого проекта: параллельные сессии в других папках не смешиваются."""
    own = PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(Path(proj).resolve()))
    mains = sorted(own.glob("*.jsonl") if own.is_dir() else PROJECTS.glob("*/*.jsonl"),
                   key=lambda p: p.stat().st_mtime)
    if not mains:
        return []
    main = mains[-1]
    sub = list((main.parent / main.stem).glob("**/*.jsonl"))
    return [main] + sub


def tally(proj, since):
    seen, out_tok, in_tok, calls = set(), 0, 0, 0
    for f in session_files(proj):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message")
            if not isinstance(m, dict) or not m.get("usage") or not d.get("timestamp"):
                continue
            if parse_ts(d["timestamp"]) < since:
                continue
            key = m.get("id") or d.get("uuid")
            if key in seen:
                continue
            seen.add(key)
            u = m["usage"]
            calls += 1
            out_tok += u.get("output_tokens", 0)
            in_tok += u.get("input_tokens", 0)
    return calls, out_tok, in_tok


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    proj, cmd = sys.argv[1], sys.argv[2]
    ensure(proj)
    path = db(proj) / "usage.jsonl"
    if cmd == "mark":
        label = sys.argv[3] if len(sys.argv) > 3 else "mark"
        append_jsonl(path, {"ts": now(), "label": label})
        print(f"отметка «{label}»")
        return
    marks = read_jsonl(path)
    if not marks:
        raise SystemExit("нет отметок — сначала usage.py <проект> mark start")
    label = sys.argv[3] if len(sys.argv) > 3 else None
    marks = [m for m in marks if m.get("label")]          # строки отчётов сюда же пишутся, у них label нет
    if not marks:
        raise SystemExit("нет отметок — сначала usage.py <проект> mark start")
    mk = next((m for m in reversed(marks) if m["label"] == label), None) if label else marks[-1]
    if not mk:
        raise SystemExit(f"нет отметки «{label}»")
    since = parse_ts(mk["ts"])
    calls, out_tok, in_tok = tally(proj, since)
    mins = (datetime.now(since.tzinfo) - since).total_seconds() / 60
    line = f"⏱ {mins:.0f} мин · {calls} вызовов модели · {out_tok / 1000:.0f}k токенов ответа · {in_tok / 1000:.1f}k новых входных"
    append_jsonl(path, {"ts": now(), "report_since": mk["label"], "minutes": round(mins, 1), "calls": calls,
                        "output_tokens": out_tok, "input_tokens": in_tok})
    print(f"с «{mk['label']}»: {line}")


if __name__ == "__main__":
    main()
