"""Риски публикации по расшифровке: сноски (Meta и пр.), мат, прочие триггеры.

  python3 risks.py <проект> [--rules <путь к publish-risks.json>]

Ищет по словам расшифровки регулярки из rules/publish-risks.json, собирает попадания с
таймкодом, цитатой-контекстом и категорией. Пишет 00_db/risks.jsonl и печатает таблицу.
Иноагентов и запрещённые организации не хардкодим — имена из расшифровки сверяет монтажёр.
"""
import json, re, sys
from pathlib import Path
from common import SKILL, db, ensure, fmt_t, journal, now, read_jsonl, write_json


def main(proj, rules_path=None):
    root = Path(proj).resolve()
    ensure(root)
    rules = json.loads(Path(rules_path or SKILL / "rules" / "publish-risks.json").read_text(encoding="utf-8"))
    words = read_jsonl(db(root) / "transcript" / "mix.words.jsonl")
    segs = read_jsonl(db(root) / "transcript" / "mix.segments.jsonl")
    if not words:
        raise SystemExit("нет mix.words.jsonl — сначала transcribe.py")

    run, hits = now(), []
    for r in rules["rules"]:
        rx = re.compile(r["pattern"], re.I)
        for i, w in enumerate(words):
            if not rx.search(w["w"].lower()):
                continue
            ctx = " ".join(x["w"] for x in words[max(0, i - 9):i + 10])
            seg = next((s for s in segs if s["t"] <= w["t"] <= s["end"]), None)
            hits.append({"t": w["t"], "category": r["category"], "word": w["w"], "mic": w.get("mic"),
                         "footnote": r.get("footnote", ""), "context": ctx,
                         "segment": seg["text"] if seg else None})
    hits.sort(key=lambda h: h["t"])

    with open(db(root) / "risks.jsonl", "w", encoding="utf-8") as f:
        for h in hits:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")

    by_cat = {}
    for h in hits:
        by_cat.setdefault(h["category"], []).append(h)
    if hits:
        print(f"Риски публикации: {len(hits)} попаданий\n")
        for cat, lst in by_cat.items():
            fn = lst[0]["footnote"]
            print(f"## {cat} — {len(lst)}" + (f" · сноска: {fn}" if fn else ""))
            for h in lst[:40]:
                print(f"  {fmt_t(h['t'])}  {h['mic']}  «{h['word']}»  — …{h['context'][:110]}…")
            print()
    else:
        print("Рисков по словарю не найдено.")
    print("⚠ Иноагенты и запрещённые организации в словарь не зашиты — имена людей и организаций "
          "из расшифровки сверь с актуальным реестром вручную.")
    seq = journal(root, "risks", hits=len(hits), categories={k: len(v) for k, v in by_cat.items()})
    print(f"\n[seq {seq}] → 00_db/risks.jsonl")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    a = sys.argv
    main(a[1], rules_path=(a[a.index("--rules") + 1] if "--rules" in a else None))
