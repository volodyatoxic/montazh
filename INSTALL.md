# Установка — инструкция для Claude Code

Ты ставишь скилл монтажа на компьютер монтажёра. Иди по шагам, ничего не спрашивай без нужды; в конце — короткий отчёт: что поставлено, что осталось сделать человеку.

## 1. Клон репозитория

- Если `~/.claude/skills/montazh` уже есть и это ссылка на клон — используй тот клон: `git -C <клон> pull --ff-only`.
- Иначе клонируй в `~/Repository/montazh` (Windows: `%USERPROFILE%\Repository\montazh`):
  `git clone https://github.com/volodyatoxic/montazh.git ~/Repository/montazh`
- Нет git: Mac — `xcode-select --install` (человек подтверждает окно) или `brew install git`; Windows — `winget install -e --id Git.Git`.

## 2. Установщик

```
python3 ~/Repository/montazh/tools/install.py
```
Windows: `py %USERPROFILE%\Repository\montazh\tools\install.py`. Нет Python: Mac — `xcode-select --install`, Windows — `winget install -e --id Python.Python.3.12`.

Установщик сам: подключает скилл в `~/.claude/skills/montazh` (старую копию отодвигает рядом, не удаляет), ставит ffmpeg, numpy и mlx-whisper, качает модель распознавания (~1,6 ГБ, несколько минут — запускай в фоне), проверяет DaVinci Resolve, git и GitHub CLI. Повторный запуск безопасен.

## 3. Что осталось

Установщик заканчивает списком «ОСТАЛОСЬ». Что можешь — сделай сам и перезапусти установщик. Что может только человек — скажи ему простыми словами, по одному шагу:

- **Resolve:** открыть DaVinci Resolve Studio → Preferences → System → General → External scripting using = **Local** → перезапустить Resolve.
- **GitHub** (нужен, чтобы отправлять правки в скилл): завести аккаунт на github.com, если нет; потом в терминале `gh auth login` → GitHub.com → HTTPS → войти через браузер. Пароли и коды человек вводит сам.
- **git:** `git config --global user.name "Имя Фамилия"` и `git config --global user.email почта` — спроси имя и почту у человека.

Проверка в конце: `python3 tools/install.py --check` — все пункты ✅ (Resolve при проверке должен быть открыт).

## 4. Скажи человеку

- Как запускать: открыть новую сессию Claude Code **в папке съёмки** и написать `/montazh разбери материал и сделай первый драфт`.
- Обновлять руками не нужно: при каждом запуске скилл сверяется с GitHub и подтягивает новую версию.
- Хочешь научить скилл чему-то для всей студии — скажи агенту «запомни это в скилл»: он отправит правку на проверку (CONTRIBUTING.md).
