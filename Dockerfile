FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY src ./src

RUN pip install --no-cache-dir beautifulsoup4==4.12.3

ENTRYPOINT ["python", "-m", "spending_mapping"]
