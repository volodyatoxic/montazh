"""Проверка обновлений скилла: сверка клона с GitHub и обновление, если можно.

  python3 update.py            — проверить и обновить (то же делает agent_init.py на старте)
  python3 update.py --check    — только проверить, ничего не менять

Обновляет только безопасно: ветка main, нет незакоммиченных правок, локальных коммитов нет.
Иначе — говорит, что мешает (обычно неотправленные правки: см. references/contributing.md).
"""
import subprocess, sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
BRANCH = "main"


def git(*args, timeout=20):
    r = subprocess.run(["git", "-C", str(SKILL), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, r.stdout.strip()


def version():
    p = SKILL / "VERSION"
    return p.read_text().strip() if p.exists() else "dev"


def news(old):
    """Строки CHANGELOG.md, которых не было в старой версии."""
    _, before = git("show", f"{old}:skills/podcastnaya-montage/CHANGELOG.md")
    now = (SKILL / "CHANGELOG.md").read_text(encoding="utf-8") if (SKILL / "CHANGELOG.md").exists() else ""
    return "\n".join(l for l in now.splitlines() if l not in set(before.splitlines())).strip()


def check(apply=True):
    """→ строка для человека или None, если всё актуально. Сеть недоступна — молчит."""
    code, top = git("rev-parse", "--show-toplevel")
    if code:
        return "⚠ скилл установлен не из git — обновлять нечем. Переустанови: склонируй репозиторий и запусти tools/install.py."
    try:
        code, _ = git("fetch", "--quiet", "origin", BRANCH)
    except subprocess.TimeoutExpired:
        return None
    if code:
        return None
    _, branch = git("rev-parse", "--abbrev-ref", "HEAD")
    _, counts = git("rev-list", "--left-right", "--count", f"HEAD...origin/{BRANCH}")
    ahead, behind = (int(x) for x in counts.split())
    _, dirty = git("status", "--porcelain")
    if not behind:
        if ahead and branch == BRANCH:
            return f"ℹ в скилле {ahead} неотправленных коммитов на {BRANCH} — отправь (git push или references/contributing.md)."
        return None
    why = []
    if branch != BRANCH:
        why.append(f"ты на ветке {branch}, а не {BRANCH}")
    if dirty:
        why.append("есть неотправленные правки в файлах скилла")
    if ahead:
        why.append(f"{ahead} локальных коммитов разошлись с GitHub")
    if why or not apply:
        tail = f": {'; '.join(why)}. Отправь правки по references/contributing.md, потом обнови." if why else "."
        return f"⬆ на GitHub новая версия скилла (+{behind} коммитов), не обновляю{tail}"
    old_v, (_, old) = version(), git("rev-parse", "HEAD")
    code, out = git("merge", "--ff-only", f"origin/{BRANCH}")
    if code:
        return f"⚠ обновить не вышло: {out[-300:]}"
    _, changed = git("diff", "--name-only", old, "HEAD")
    msg = [f"⬆ СКИЛЛ ОБНОВЛЁН {old_v} → {version()}. Перечитай SKILL.md — в контексте старая версия."]
    if any(f.startswith("tools/install.py") for f in changed.splitlines()):
        msg.append("Изменился установщик — запусти tools/install.py из репозитория скилла.")
    n = news(old)
    if n:
        msg.append("Что нового:\n" + n)
    return "\n".join(msg)


if __name__ == "__main__":
    print(check(apply="--check" not in sys.argv) or f"скилл {version()} — актуальный")
