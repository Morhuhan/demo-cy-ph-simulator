# Образ модуля распознавания (CV-Module) под Linux с видеокартой NVIDIA.
#
# Собирается из папки с кодом модуля — в архиве для передачи это cv-module/,
# куда tools/pack_stand.py кладёт этот файл под именем Dockerfile.
# Веса модели в образ не входят: они подключаются томом, и сменить их можно
# без пересборки.
#
# Без видеокарты: --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cpu
# и device: cpu в docker/cv-config.yaml.

# bookworm, а не плавающий slim: в trixie пакет glib переименован, и сборка
# сломалась бы при очередном обновлении базового образа.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    YOLO_CONFIG_DIR=/tmp/ultralytics

# OpenCV из pip требует libGL и glib даже без окна на экране.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch и torchvision — вместе и с одного индекса: с PyPI приехала бы
# сборка без CUDA. Самый тяжёлый слой, около 3 ГБ, поэтому идёт первым и
# не пересобирается при правке кода.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu126
RUN pip install torch torchvision --index-url ${TORCH_INDEX}

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src ./src
COPY app.py ./

CMD ["python", "app.py", "--config", "/app/config.docker.yaml", "--source", "0", "--no-window", "--preview-port", "8081"]
