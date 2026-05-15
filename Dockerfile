FROM python:3.12-slim

# bgpq4 is in Debian/Ubuntu repos.
RUN apt-get update \
 && apt-get install -y --no-install-recommends bgpq4 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=8080
EXPOSE 8080

# 2 workers is plenty for a thin bgpq4 proxy; tune via GUNICORN_WORKERS if needed.
ENV GUNICORN_WORKERS=2
ENV GUNICORN_THREADS=4

CMD ["sh", "-c", "exec gunicorn -w ${GUNICORN_WORKERS} --threads ${GUNICORN_THREADS} -b 0.0.0.0:${PORT} --access-logfile - app:app"]
