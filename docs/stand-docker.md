# Стенд и CV-модуль в Docker

Два варианта, выбор — по системе:

| Система | Файл | Что в контейнерах |
|---|---|---|
| Linux с камерой и NVIDIA | `docker-compose.full.yml` | стенд и CV-модуль |
| Windows, macOS | `docker-compose.stand.yml` | только стенд, CV-модуль на машине |

Под Windows и macOS Docker работает в виртуальной машине и не пробрасывает в
контейнер камеру, поэтому там CV-модуль запускается как обычно, рядом.

## Всё в Docker (Linux)

```text
/dev/video0 → контейнер cv ──ws://stand:8080/cv?stand=demo──▶ контейнер stand
                   │                                              │
                   └─ 127.0.0.1:8081/video ─▶ браузер ◀─ 127.0.0.1:8080/check
```

Нужно:

* Docker с Compose 2.20 или новее (`docker compose version`);
* драйвер NVIDIA 525+ и NVIDIA Container Toolkit — проверка:
  `docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu22.04 nvidia-smi`;
* камера на `/dev/video0` (`ls /dev/video*`).

```bash
docker compose -f docker-compose.full.yml up --build -d
```

Первая сборка — 10–20 минут: образ CV-модуля тянет PyTorch с CUDA, около
3 ГБ. Дальше открыть `http://localhost:8080/check` — видео с рамками, вердикты
и голос на одной странице. Состояние стыка — `curl http://127.0.0.1:8080/cv/status`.

Код CV-модуля в `cv-module/` — CV-Module на коммите `e18a8e6` плюс патч видео
для страницы (`docs/cv-preview.patch`). Веса `cv-module/models/` и
конфигурация `docker/cv-config.yaml` подключены томами: менять их можно без
пересборки, хватит `docker compose -f docker-compose.full.yml restart cv`.
Собрать образ из своего репозитория — положить в него `cv-module/Dockerfile`
и `.dockerignore` и в `docker-compose.full.yml` заменить `build: ./cv-module`
на путь к репозиторию.

Источник видео — в файле `.env` рядом с compose-файлом:

| Что | В `.env` |
|---|---|
| другая камера | `CV_CAMERA=/dev/video2` |
| ролик из папки `videos/` | `CV_SOURCE=/videos/test.mp4` |
| RTSP-камера | `CV_SOURCE=rtsp://…` |

Для ролика и RTSP строку `devices` в `docker-compose.full.yml` убрать: с
несуществующей камерой контейнер не запустится. Без видеокарты — собрать с
`--build-arg TORCH_INDEX=https://download.pytorch.org/whl/cpu`, в
`docker/cv-config.yaml` поставить `device: cpu` и убрать блок `deploy`; на
процессоре модель заметно медленнее норматива.

Журналы: `docker compose -f docker-compose.full.yml logs -f cv` (или `stand`).
Остальное — как ниже, только с `-f docker-compose.full.yml`.

## Только стенд в Docker (Windows, macOS)

```text
камера → CV-модуль (на машине) ──ws://127.0.0.1:8080/cv?stand=demo──▶ стенд (контейнер)
                │                                                         │
                └── http://127.0.0.1:8081/video ──▶ браузер ◀── http://127.0.0.1:8080/check
```

### Что нужно

* Docker с Compose v2 (Docker Desktop под Windows или macOS).
* CV-Module на коммите `e18a8e6` или новее: время кадра, `event_id` и признак
  жизни уже там, стенд их ждёт.
* Порты 8080 (стенд) и 8081 (видео от CV-модуля) свободны.

### 1. Поднять стенд

```bash
docker compose -f docker-compose.stand.yml up --build -d
```

Первая сборка занимает пару минут: образ на `python:3.12-slim`, без torch.
Проверить, что стенд жив:

```bash
curl http://127.0.0.1:8080/cv/status
```

В ответе `"enabled": true` и `"clients": 0` — стенд работает и ждёт CV-модуль.

### 2. Подключить CV-модуль

В `config.yaml` CV-модуля адрес стенда уже правильный:

```yaml
transport:
  type: websocket
  url: ws://127.0.0.1:8080/cv?stand=demo
```

Параметр `stand=demo` обязателен: без него стенд закрывает соединение кодом
4401. Ключ `demo` задан в `docker-compose.stand.yml`, поэтому браузер и
CV-модуль попадают в одно занятие, в каком бы порядке их ни запустить.

Запуск — как обычно, источник любой:

```bash
python app.py --source 0 --no-window              # камера
python app.py --source test.mp4 --no-window       # ролик
```

Снова `curl http://127.0.0.1:8080/cv/status`: `clients` стал 1,
`service_events` растёт на единицу в секунду (признак жизни), а с человеком
в кадре растёт и `events`, в `subjects` появляется `person-N`.

### 3. Видео в странице проверки

Страница `http://127.0.0.1:8080/check` показывает кадр с рамками, который ей
отдаёт CV-модуль на `http://127.0.0.1:8081/video`. В CV-Module этого пока
нет — это патч `docs/cv-preview.patch`: новый `src/preview.py` (MJPEG без
внешних зависимостей), секция `preview` в конфигурации и ключ
`--preview-port`. Распознавание и события он не трогает.

```bash
cd CV-Module
git apply путь/к/docs/cv-preview.patch
python -m pytest                                   # 49 тестов
python app.py --source 0 --no-window --preview-port 8081
```

Без патча стенд работает так же, просто на странице не будет картинки.

## Провести проверку

Открыть `http://localhost:8080/check` в Chrome, нажать «Начать проверку».
Сценарий СИЗ-01 (`configs/sop/ppe-01.yaml`):

1. На предъявление каски — 8 секунд. Каска на голове и уверенность от 0,7 —
   проверка пройдена; нет — «допуск запрещён».
2. Доклад о готовности голосом («к работе готов») или кнопкой.
3. Каска снята после проверки — критическое нарушение.

Полный интерфейс инструктора с журналом и отчётами —
`http://localhost:8080/`.

Реплики стенда записаны заранее и проигрываются браузером. Голосовые
команды обучаемого распознаются через Hugging Face — нужен свой токен с
правом «Make calls to Inference Providers» в файле `.env` рядом с
`docker-compose.stand.yml`:

```text
HF_TOKEN=hf_...
```

Без токена всё работает, команды остаются кнопками инструктора.

## Повседневное

| Что | Команда |
|---|---|
| журнал стенда | `docker compose -f docker-compose.stand.yml logs -f` |
| после правки порогов | `docker compose -f docker-compose.stand.yml restart` |
| после правки кода стенда | `docker compose -f docker-compose.stand.yml up --build -d` |
| остановить | `docker compose -f docker-compose.stand.yml down` |
| остановить и стереть журнал занятий | `docker compose -f docker-compose.stand.yml down -v` |

Пороги сглаживания и СОП лежат в `configs/` и подключены в контейнер
снаружи, пересобирать образ не нужно. Сглаживание стенда —
`configs/app-ppe-live.yaml`, секция `smoothing.variables.helmet`; оно
работает вторым слоем поверх стабилизатора CV-модуля.

## Если CV-модуль не на этой машине

Порт стенда открыт только для этой машины: авторизации у стенда нет. Для
CV-модуля на другой машине в `docker-compose.stand.yml` заменить
`"127.0.0.1:8080:8080"` на `"8080:8080"`, а в `transport.url` поставить адрес
машины со стендом. Странице проверки тогда нужно сказать, откуда брать видео:
`http://<стенд>:8080/check?video=http://<машина CV>:8081/video`, а у CV-модуля
в секции `preview` поставить `host: 0.0.0.0`.

## Что ещё не согласовано

Расхождения контрактов и открытые вопросы — в
[integration-cv.md](integration-cv.md). Главное для отладки: стенд пока не
считает молчание CV-модуля потерей потока (`heartbeat_expected: false` в
`configs/app-ppe-live.yaml`), хотя признак жизни уже приходит. Зависший
модуль с живым сокетом стенд не заметит.
