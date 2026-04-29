FROM python:3.13-slim

WORKDIR /app

COPY download/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY VERSION .
COPY download/ download/

# Read version and set as env var for Flask app.
RUN echo "APP_VERSION=$(cat VERSION)" > /app/version.env

ENV PORT=8080
EXPOSE 8080

CMD set -a && . /app/version.env && set +a && gunicorn download.server:app --bind "0.0.0.0:${PORT}"
