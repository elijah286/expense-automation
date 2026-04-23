FROM python:3.13-slim

WORKDIR /app

COPY download/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY download/ download/

ENV PORT=8080
EXPOSE 8080

CMD gunicorn download.server:app --bind "0.0.0.0:${PORT}"
