"""Проверка репозитория перед отправкой правок (и в GitHub Actions на каждый PR).

  python3 tools/check.py

Репозиторий публичный: сюда не попадают медиа, расшифровки, базы проектов, крупные файлы,
название студии и имена.
Плюс скрипты компилируются, JSON читается, у SKILL.md есть name и description.
"""
import hashlib, json, re, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAX_BYTES = 1_000_000
MEDIA = {".mp4", ".mov", ".mxf", ".mkv", ".avi", ".braw", ".r3d", ".wav", ".mp3", ".m4a", ".aac", ".flac",
         ".aif", ".aiff", ".drp", ".drt", ".npy", ".jsonl", ".srt", ".vtt"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
TEXT = {".md", ".py", ".json", ".xml", ".yml", ".yaml", ".txt"}
# Начала слов, которых не должно быть в публичном репо (название, имена, проекты), — хэшами, чтобы не светить сами слова.
# (длина начала, sha256[:16], только с заглавной)
BANNED = [(8, "7f6aef60216f7d13", True),
           (5, "13e9b89314f40aa7", False),
           (7, "85220a351bd0ff4e", False),
           (7, "5044781b625680b8", False),
           (8, "00add219b3aa79dd", False),
           (6, "bb48df7dda0c8217", False),
           (10, "7fcf6f5210f60fd9", False)]


def files():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         capture_output=True, text=True, encoding="utf-8").stdout
    return [REPO / f for f in out.split("\0") if f and (REPO / f).is_file()]


def banned_word(text):
    for m in re.finditer(r"\w+", text):
        w = m.group()
        for n, digest, cap in BANNED:
            if len(w) >= n and (w[0].isupper() or not cap) and \
                    hashlib.sha256(w[:n].lower().encode()).hexdigest()[:16] == digest:
                return w


def main():
    errors = []
    for p in files():
        rel = p.relative_to(REPO).as_posix()
        ext = p.suffix.lower()
        if "_agent/" in rel or ext in MEDIA:
            errors.append(f"{rel}: медиа/данные проекта в публичный репозиторий не кладём")
        elif ext in IMAGES and not rel.startswith("docs/"):
            errors.append(f"{rel}: картинки — только в docs/ и без клиентских кадров")
        if p.stat().st_size > MAX_BYTES:
            errors.append(f"{rel}: {p.stat().st_size // 1024} КБ — больше 1 МБ")
        bad = banned_word(rel) or (banned_word(p.read_text(encoding="utf-8", errors="ignore")) if ext in TEXT else None)
        if bad:
            errors.append(f"{rel}: «{bad}» — названий студии, имён и проектов в публичном репо быть не должно")
        if ext == ".py":
            try:
                compile(p.read_text(encoding="utf-8"), rel, "exec")
            except SyntaxError as e:
                errors.append(f"{rel}:{e.lineno}: не компилируется — {e.msg}")
        if ext == ".json":
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except ValueError as e:
                errors.append(f"{rel}: битый JSON — {e}")
    skill = REPO / "skills" / "podcastnaya-montage" / "SKILL.md"
    head = skill.read_text(encoding="utf-8").split("---")[1] if skill.exists() else ""
    if "name:" not in head or "description:" not in head:
        errors.append("SKILL.md: во frontmatter нет name или description")
    if errors:
        print("✖ " + "\n✖ ".join(errors))
        sys.exit(1)
    print("✅ проверка пройдена")


if __name__ == "__main__":
    main()
