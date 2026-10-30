"""Установка скилла podcastnaya-montage на компьютер монтажёра. Повторный запуск безопасен.

  python3 tools/install.py              (Windows: py tools\\install.py)
  python3 tools/install.py --check      — только проверить, ничего не ставить
  python3 tools/install.py --no-model   — не качать модель распознавания (~1,6 ГБ)

Делает: подключает скилл в ~/.claude/skills, ставит ffmpeg, numpy, pillow, шрифт Inter и распознавание речи,
качает модель, проверяет DaVinci Resolve, git и gh. В конце — что осталось сделать человеку.
"""
import os, platform, re, shutil, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "podcastnaya-montage"
LINK = Path.home() / ".claude" / "skills" / "podcastnaya-montage"
MAC, WIN = platform.system() == "Darwin", platform.system() == "Windows"
APPLE_SILICON = MAC and platform.machine() == "arm64"
CHECK = "--check" in sys.argv

sys.path.insert(0, str(SKILL / "scripts"))
from common import _resolve_paths  # noqa: E402

done, todo = [], []


def ok(text):
    done.append(f"✅ {text}")


def need(text):
    todo.append(text)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def same_dir(a, b):
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def link_skill():
    if same_dir(LINK, SKILL):
        return ok(f"скилл подключён: {LINK} → {SKILL}")
    if CHECK:
        return need(f"подключить скилл: {LINK} → {SKILL} (запусти install.py без --check)")
    LINK.parent.mkdir(parents=True, exist_ok=True)
    if LINK.exists() or LINK.is_symlink():
        backup = LINK.with_name(f"{LINK.name}.old-{time.strftime('%y%m%d-%H%M')}")
        LINK.rename(backup)
        print(f"старая версия скилла отодвинута в {backup}")
    if WIN:
        r = run(["cmd", "/c", "mklink", "/J", str(LINK), str(SKILL)])
        if r.returncode:
            return need(f"не вышло подключить скилл: {r.stdout}{r.stderr}")
    else:
        LINK.symlink_to(SKILL, target_is_directory=True)
    ok(f"скилл подключён: {LINK} → {SKILL}")


def pip_install(pkgs):
    base = [sys.executable, "-m", "pip", "install", "--user", "--upgrade", *pkgs]
    r = run(base)
    if r.returncode and "externally-managed" in (r.stdout + r.stderr):
        r = run(base + ["--break-system-packages"])
    return r


def python_deps():
    if sys.version_info < (3, 9):
        return need(f"Python {platform.python_version()} слишком старый — нужен 3.9+")
    wanted = {"numpy": "numpy", "PIL": "pillow"}
    if APPLE_SILICON:
        wanted["mlx_whisper"] = "mlx-whisper"
    missing = []
    for mod, pkg in wanted.items():
        if run([sys.executable, "-c", f"import {mod}"]).returncode:
            missing.append(pkg)
    if missing and not CHECK:
        print(f"ставлю {', '.join(missing)}…", flush=True)
        r = pip_install(missing)
        if r.returncode:
            return need(f"pip не поставил {', '.join(missing)}:\n{(r.stdout + r.stderr)[-600:]}")
        missing = [p for m, p in wanted.items() if run([sys.executable, "-c", f"import {m}"]).returncode]
    if missing:
        return need(f"поставить Python-пакеты: {', '.join(missing)}")
    ok(f"Python {platform.python_version()} ({sys.executable}): {', '.join(wanted.values())}")
    if not APPLE_SILICON:
        need("расшифровка пока только для Mac на M-чипе (mlx-whisper). На этом компьютере всё, "
             "кроме расшифровки, работает; её делать на маке — или допиши faster-whisper в transcribe.py (references/contributing.md)")


def ffmpeg():
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return ok("ffmpeg и ffprobe")
    if not CHECK:
        if MAC and shutil.which("brew"):
            print("ставлю ffmpeg через Homebrew…", flush=True)
            run(["brew", "install", "ffmpeg"])
        elif WIN and shutil.which("winget"):
            print("ставлю ffmpeg через winget…", flush=True)
            run(["winget", "install", "-e", "--id", "Gyan.FFmpeg",
                 "--accept-source-agreements", "--accept-package-agreements"])
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return ok("ffmpeg и ffprobe (поставлен)")
    need("поставить ffmpeg: Mac — https://brew.sh, потом `brew install ffmpeg`; Windows — `winget install Gyan.FFmpeg`. "
         "На Windows после установки перезапусти терминал/Claude Code, чтобы обновился PATH")


def inter_font():
    dirs = [Path.home() / "Library/Fonts", Path("/Library/Fonts"),
            Path.home() / "AppData/Local/Microsoft/Windows/Fonts", Path("C:/Windows/Fonts")]
    has = lambda: any(d.exists() and any(d.glob("Inter*")) for d in dirs)
    if not has() and not CHECK and MAC and shutil.which("brew"):
        print("ставлю шрифт Inter…", flush=True)
        run(["brew", "install", "--cask", "font-inter"])
    if has():
        return ok("шрифт Inter (титры и плашки по умолчанию)")
    need("поставить шрифт Inter (бесплатный, для титров): https://rsms.me/inter/ → скачать → установить все начертания")


def whisper_model():
    if not APPLE_SILICON or "--no-model" in sys.argv:
        return
    src = (SKILL / "scripts" / "transcribe.py").read_text(encoding="utf-8")
    m = re.search(r'^MODEL\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        return need("не нашёл имя модели в transcribe.py")
    model = m.group(1)
    code = ("import sys\nfrom huggingface_hub import snapshot_download\n"
            f"print(snapshot_download('{model}', local_files_only={CHECK}))")
    if not CHECK:
        print(f"качаю модель распознавания {model} (~1,6 ГБ, один раз)…", flush=True)
    r = run([sys.executable, "-c", code])
    if r.returncode:
        return need(f"скачать модель {model}: запусти install.py без --check" if CHECK
                    else f"модель {model} не скачалась:\n{(r.stdout + r.stderr)[-400:]}")
    ok(f"модель распознавания {model}")


def resolve():
    api, lib = _resolve_paths()
    if not Path(api, "Modules", "DaVinciResolveScript.py").exists() or not Path(lib).exists():
        return need("DaVinci Resolve Studio не найден. Нужен Studio 19+ (в бесплатной 21.1+ скрипты убраны)")
    ok("DaVinci Resolve установлен, модуль скриптов на месте")
    code = ("import os, sys\n"
            f"os.environ['RESOLVE_SCRIPT_API'], os.environ['RESOLVE_SCRIPT_LIB'] = {api!r}, {lib!r}\n"
            f"sys.path.append({str(Path(api, 'Modules'))!r})\n"
            "import DaVinciResolveScript as d\nr = d.scriptapp('Resolve')\n"
            "print(f'{r.GetProductName()} {r.GetVersionString()}' if r else '')")
    try:
        r = run([sys.executable, "-c", code], timeout=20)
        name = r.stdout.strip()
    except subprocess.TimeoutExpired:
        name = ""
    if name:
        ok(f"Resolve отвечает: {name}")
        if "Studio" not in name:
            need(f"это {name}, а нужен DaVinci Resolve Studio")
    else:
        need("Resolve: открой его и проверь Preferences → System → General → External scripting using = Local "
             "(если окно обновления — закрой его). Потом можно перепроверить: install.py --check")


def git_and_gh():
    name = run(["git", "config", "user.name"]).stdout.strip()
    email = run(["git", "config", "user.email"]).stdout.strip()
    if name and email:
        ok(f"git: {name} <{email}>")
    else:
        need("представиться git (для отправки правок в скилл): "
             "`git config --global user.name \"Имя Фамилия\"` и `git config --global user.email почта`")
    if not shutil.which("gh") and not CHECK:
        if MAC and shutil.which("brew"):
            run(["brew", "install", "gh"])
        elif WIN and shutil.which("winget"):
            run(["winget", "install", "-e", "--id", "GitHub.cli",
                 "--accept-source-agreements", "--accept-package-agreements"])
    if not shutil.which("gh"):
        return need("поставить GitHub CLI (`gh`) — нужен, чтобы отправлять правки в скилл: https://cli.github.com")
    if run(["gh", "auth", "status"]).returncode:
        return need("войти в GitHub — человек сам в терминале: `gh auth login` (GitHub.com → HTTPS → браузер). "
                    "Аккаунта нет — заведи на github.com. Без этого скилл работает, но правки в него не отправить")
    ok("GitHub CLI: вход выполнен")


def main():
    print(f"podcastnaya-montage {(SKILL / 'VERSION').read_text().strip()} · {REPO}\n", flush=True)
    for step in (link_skill, python_deps, ffmpeg, inter_font, whisper_model, resolve, git_and_gh):
        step()
    print("\n".join(done))
    if todo:
        print("\nОСТАЛОСЬ (что можешь — сделай сам и перезапусти install.py; что может только человек — объясни ему по шагу):")
        print("\n".join(f"• {t}" for t in todo))
    else:
        print("\nВсё готово.")
    print("\nСКАЖИ ЧЕЛОВЕКУ:\n"
          "• запуск — новая сессия Claude Code в папке съёмки: /podcastnaya-montage разбери материал и сделай первый драфт\n"
          "• обновлять руками не нужно: при каждом запуске скилл сверяется с GitHub и подтягивает новую версию\n"
          "• «запомни это в скилл» — агент отправит правку на проверку для всех")


if __name__ == "__main__":
    main()
