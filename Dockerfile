FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SIGNAL_DB_PATH=/home/prism/data/prism_signals.db \
    PORT=10000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# TA-Lib 0.6.x publishes manylinux wheels containing the native library.
RUN pip install --upgrade pip \
    && pip install --only-binary=TA-Lib -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 prism \
    && mkdir -p /home/prism/data \
    && chown -R prism:prism /home/prism/data
USER prism

EXPOSE 10000
CMD ["python", "-m", "app.main"]
