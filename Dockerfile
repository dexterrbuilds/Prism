FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000

WORKDIR /app

COPY requirements.txt .
# TA-Lib 0.6.x publishes manylinux wheels containing the native library.
RUN pip install --upgrade pip \
    && pip install --only-binary=TA-Lib -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 prism
USER prism

EXPOSE 10000
CMD ["python", "-m", "app.main"]
