# DaVinci Resolve через API

Подключение — `common.connect()` (сам находит пути API на macOS и Windows). Если `scriptapp` вернул None или методы отдают пустое — Resolve не запущен или висит модальный диалог (часто — окно обновления после запуска: нажать Skip). Справка API: `README.txt` в папке Developer/Scripting — грепай её, прежде чем утверждать, что чего-то нет.

## Версии
Первым делом `GetProductName()` + `GetVersionString()` → расклад человеку.
| Версия | Что можем |
|---|---|
| 19–21.0 Studio | наши скрипты через внешний Python API (проверено на 20.0) |
| 21.1+ Studio | то же + встроенный MCP (File ▸ Setup AI Assistants) + ~20 новых вызовов: `GetTranscription`, мультикам/SmartSwitch, автовыравнивание, переходы, фейды, скорость |
| 21.1+ бесплатный | Python-скриптинг убран — работать нельзя (в студии везде Studio) |
| совсем старая | таймлайн XML-файлом для импорта + предупреждение |
MCP не даёт суперсил: тримить/резать клипы он тоже не умеет, каждое действие — вызов модели. Пакетные операции — скриптами.

## Таймлайны
- `mp.AppendToTimeline([{mediaPoolItem, startFrame, endFrame, recordFrame, trackIndex, mediaType}])` — метод MediaPool. `endFrame` исключающий; `startFrame` — кадр файла; видео и звук раздельно (`mediaType` 1/2). Порциями по 40–60.
- Клип 50p на таймлайне 25p: кадры в единицах клипа, длина ×2. Звук WAV — в кадрах таймлайна.
- `GetSourceStartFrame` округляет вниз — сверяй ±1 кадр.
- `DuplicateTimeline(name)` (имя без «:») сохраняет Fairlight, стили субтитров, грейды. `CreateEmptyTimeline` — без них.
- `SetTrackEnable` действует на текущий таймлайн — сначала `SetCurrentTimeline`.
- Удалить клипы **с таймлайна** — `tl.DeleteClips(items, ripple)`. Ripple одного клипа сдвигает все дорожки и маркеры.
- **Никогда** `mp.DeleteClips` для клипов, стоящих на таймлайнах: виснет диалог, старые таймлайны ломаются.
- `ImportMedia` — по одному файлу (пачка PNG склеивается в секвенцию). Картинка импортируется длиной 1 кадр — для удержаний делай видео.
- Разрезать/тримить клип API не умеет: вырез = два куска + удаление пустышки с ripple (`cut_range.py` из podcast-montage).

## Маркеры
- `tl.AddMarker(frame, color, name, note, duration, customData)`; ключи `GetMarkers()` — от начала таймлайна. На одном кадре два маркера не встанут — сдвигай на +1.
- `GetMarkers()` отдаёт `color, name, note, duration (кадры), customData`. Маркеры на клипах — `item.GetMarkers()` отдельно.
- В `customData` пиши id записи из базы.

## Рендер
- `LoadRenderPreset`, `SetRenderSettings({SelectAllFrames, TargetDir, CustomName, ExportVideo, ExportAudio, MarkIn, MarkOut, ...})`, `AddRenderJob`, `StartRendering([job])`, ждать `IsRenderingInProgress()`.
- **Главы (проверено на 20.0 Studio, 30.10.2026):** галка «Chapters from Markers» в Deliver берёт маркеры одного цвета, название главы = имя маркера, время точное. Нет маркера на 0:00 — Resolve добавит главу «Start». В API ключа нет, но в XML пресета есть `ExtraInfoMap/marker_chapters_color = ResolveColorPurple`. Наш пресет: `presets/chapters purple.xml` → `resolve.ImportRenderPreset(path)` (метод объекта resolve, имя пресета = имя файла). Экспорт системных пресетов не работает — только пользовательских.
- Звук для проверки: `ExportVideo: False` (~12 с на 25 мин). Resolve запоминает это — перед видео-рендером явно `ExportVideo: True`, после рендера проверь ffprobe, что есть видеопоток.
- Рендер стартует не с первого раза — повтори; для аудио нужна открытая страница Deliver (`r.OpenPage("deliver")`).
- Скорость: минута 1080p25 H.264 ≈ 6–7 с на M-маке (тестовая картинка).

## Resolve один на всех
Перед работой: `IsRenderingInProgress()`, какой проект открыт. Чужой проект — сохранить, загрузить свой, потом вернуть. Не трогай Resolve, пока человек режет (API отдаёт полуизменённые клипы). `ExportProject` может вернуть False, если человек на странице Fairlight или в диалоге.

## Чего API не умеет
Fairlight (EQ, динамика, громкость дорожки), стиль субтитров, скорость клипа, выделенные клипы, ключевые кадры трансформа. Это человек или computer-use.

## Прочие грабли
- FCP7 XML с камерными MOV без `<timecode>` → Media Offline; причину смотри в `ResolveDebug.txt`.
- Перезапись медиафайла по тому же пути при открытом проекте → offline.
- `ImportTimelineFromFile` с тяжёлым исходником в открытом проекте может повесить API.
- `GrabStill` после `SetCurrentTimecode` отдаёт предыдущий кадр — пауза ≥ 2 с.

## Грабли первой боевой съёмки (30.10.2026, Resolve 20.0 Studio)
- `ImportProject(drp, name)` **игнорирует имя**: проект появляется под внутренним именем из `.drp` (у нас «Podcast»). Переименовывать после через `project.SetName()`. `LoadProject` с задуманным именем до этого вернёт None.
- Импорт `.drp` от ATEM Mini Pro ISO восстанавливает нарезку оператора клип в клип: 96 событий в логе → 96 клипов на V1, старт таймлайна = `masterTimecode`, кадры источника считаются от начала файла (0), а не от таймкода.
- **Звук отдельно не рендерится форматом:** в `GetRenderFormats()` нет `wav`, в кодеках mp4 только H264/H265. Только `LoadRenderPreset("Audio Only")` + `ExportVideo: False`.
- `IsRenderingInProgress()` возвращает False **до** фактического старта — цикл ожидания по нему выходит мгновенно. Ждать по `GetRenderJobStatus(job)["JobStatus"] == "Complete"`.
- **Имена файлов на macOS — NFD.** Отрендеренный «v01 смысловой звук.wav» не находится `glob` по маске с «й»: на диске это «и» + U+0306. Служебным рендерам давать ASCII-имена (это и так стандарт хранения студии для exFAT), а сравнения нормализовать через `unicodedata.normalize("NFC", ...)`.
- Позиции клипов считать **в кадрах нарастающим итогом**. Если считать в секундах и округлять каждую позицию отдельно, на V1 появляются дыры в один кадр, а звук уезжает от картинки (у нас было 44 мс).
