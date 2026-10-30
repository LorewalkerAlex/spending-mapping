FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN pip install --no-cache-dir beautifulsoup4==4.12.3

COPY src ./src
COPY config ./config

ENTRYPOINT ["python", "-m", "spending_mapping"]
