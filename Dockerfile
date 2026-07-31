FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8090

# Shell form (not exec array) so $PORT expands — Render (and some other free-tier
# hosts) assign a random port via the PORT env var. Falls back to 8090 for local/
# docker-compose runs where PORT isn't set.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8090}
