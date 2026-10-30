"""Создаёт или находит _agent/ в папке проекта.  python3 agent_init.py <проект>

Сначала сверяет скилл с GitHub и обновляет, если можно (update.py).
Новый проект → структура, README, project.json, запись «init» в журнале.
Есть _agent/ → «resume»: печатает brief.md и последние 10 записей журнала.
"""
import sys
from pathlib import Path
from common import VERSION, agent, db, ensure, journal, now, read_json, read_jsonl, write_json
import update

README = """# _agent — рабочая папка монтажёра-агента (podcastnaya-montage)

Здесь всё, что агент создал для этого проекта: база (00_db), разбор материала, синхра, расшифровки,
планы монтажа, графика, проверки. Исходники агент не трогает.

Удалить эту папку = как будто агент не работал, кроме:
- проекта и таймлайнов в DaVinci Resolve — список в 00_db/timelines.jsonl (удаляются по команде cleanup);
- рендеров и текстов для клиента в ../renders/.
"""


def main(proj):
    p = Path(proj).resolve()
    if not p.is_dir():
        raise SystemExit(f"нет папки {p}")
    upd = update.check()
    if upd:
        print(upd + "\n")
    existed = (agent(p) / "00_db" / "project.json").exists()
    ensure(p)
    if not existed:
        (agent(p) / "README.md").write_text(README, encoding="utf-8")
        write_json(db(p) / "project.json", {"name": p.name, "path": str(p), "created": now(), "skill_version": VERSION,
                                             "type": None, "scenario": None, "level": None})
        (db(p) / "brief.md").write_text(f"# {p.name}\n\nНовый проект. Следующий шаг: scan.py (исследование).\n", encoding="utf-8")
        seq = journal(p, "init", skill_version=VERSION)
        print(f"NEW  {p.name} — _agent создан (seq {seq})")
    else:
        seq = journal(p, "resume", skill_version=VERSION)
        print(f"RESUME  {p.name} (seq {seq})\n")
        print((db(p) / "brief.md").read_text(encoding="utf-8"))
        print("\nПоследние действия:")
        for e in read_jsonl(db(p) / "journal.jsonl")[-10:]:
            extra = {k: v for k, v in e.items() if k not in ("seq", "ts", "action")}
            print(f"  {e['seq']:>4} {e['ts'][:16]} {e['action']} {extra if extra else ''}")
    pj = read_json(db(p) / "project.json", {})
    if pj.get("skill_version") != VERSION:
        print(f"\n⚠ проект начат на версии скилла {pj.get('skill_version')}, сейчас {VERSION} — смотри CHANGELOG.md")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
