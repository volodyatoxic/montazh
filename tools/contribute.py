"""Отправить правки скилла в общий репозиторий студии — pull request.

  python3 tools/contribute.py "Что поменял, коротко" --body описание.md

Берёт все правки в клоне скилла, проверяет (tools/check.py), кладёт на ветку <логин>/<дата>-<тема>,
пушит (свои права на репозиторий есть — в него, нет — в форк) и открывает PR в main.
После — возвращает клон на main: дальше скилл обновится сам, когда PR примут.
"""
import argparse, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN = "main"
TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
              "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch - y - e yu ya".split()))


def sh(*cmd, check=True):
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode:
        sys.exit(f"✖ {' '.join(cmd[:3])}…: {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout.strip()


def slug(text):
    s = "".join(TR.get(c, c) for c in text.lower())
    s = "".join(c if c.isascii() and c.isalnum() else "-" for c in s)
    return "-".join(filter(None, s.replace("--", "-").split("-")))[:40] or "pravki"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("title")
    ap.add_argument("--body", help="файл с описанием PR: что, зачем, на каком материале проверено")
    a = ap.parse_args()

    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode:
        sys.exit("✖ нет входа в GitHub: человеку выполнить `gh auth login`")
    branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD")
    dirty = sh("git", "status", "--porcelain")
    sh("git", "fetch", "--quiet", "origin", MAIN, check=False)
    ahead = int(sh("git", "rev-list", "--count", f"origin/{MAIN}..HEAD", check=False) or 0)
    if not dirty and not ahead:
        sys.exit("нечего отправлять: правок нет")
    if subprocess.run([sys.executable, str(REPO / "tools" / "check.py")]).returncode:
        sys.exit("✖ проверка не прошла — поправь и запусти снова")

    login = sh("gh", "api", "user", "-q", ".login")
    upstream = sh("git", "remote", "get-url", "origin").removesuffix(".git").split("github.com")[-1].lstrip(":/")
    from_main = branch == MAIN
    if from_main:
        branch = f"{login}/{time.strftime('%y%m%d')}-{slug(a.title)}"
        sh("git", "switch", "-c", branch)
    if dirty:
        sh("git", "add", "-A")
        sh("git", "commit", "-m", a.title)

    perm = sh("gh", "repo", "view", upstream, "--json", "viewerPermission", "-q", ".viewerPermission", check=False)
    if perm in ("ADMIN", "MAINTAIN", "WRITE"):
        remote, head = "origin", branch
    else:
        if "fork" not in sh("git", "remote").split():
            sh("gh", "repo", "fork", upstream, "--clone=false", check=False)
            fork = f"{login}/{upstream.split('/')[1]}"
            sh("gh", "repo", "view", fork, "--json", "name")      # форк должен существовать
            sh("git", "remote", "add", "fork", f"https://github.com/{fork}.git")
        remote, head = "fork", f"{login}:{branch}"
    for attempt in range(3):                                      # свежий форк GitHub готовит несколько секунд
        if not subprocess.run(["git", "push", "-u", remote, "HEAD"], cwd=REPO, capture_output=True).returncode:
            break
        time.sleep(5)
    else:
        sh("git", "push", "-u", remote, "HEAD")

    body = Path(a.body).read_text(encoding="utf-8") if a.body else "Изменения:\n\n" + sh(
        "git", "diff", "--stat", f"origin/{MAIN}...HEAD")
    url = sh("gh", "pr", "create", "--repo", upstream, "--base", MAIN, "--head", head,
             "--title", a.title, "--body", body)

    if from_main:
        sh("git", "branch", "-f", MAIN, f"origin/{MAIN}")   # правки уже на ветке PR, main — как на GitHub
    sh("git", "switch", MAIN)
    print(f"✅ PR открыт: {url}\nКлон снова на {MAIN}. Правки вернутся в скилл, когда PR примут.")


if __name__ == "__main__":
    main()
