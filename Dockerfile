FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# lxml/Pillow нужны системные библиотеки
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY harvester ./harvester
COPY deploy ./deploy
COPY tests ./tests

# [embed] — модель оценки релевантности; она нужна прогонам, запущенным через API
RUN pip install --upgrade pip && pip install -e ".[dev,embed]"

EXPOSE 8080
CMD ["uvicorn", "harvester.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
