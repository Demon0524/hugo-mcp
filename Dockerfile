FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY server.py /app/server.py

RUN useradd --system --uid 10001 --create-home hugomcp

EXPOSE 8080
ENTRYPOINT ["python", "/app/server.py"]
