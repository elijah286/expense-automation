FROM python:3.13-slim

WORKDIR /app

COPY download/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY download/ download/

EXPOSE 8080

CMD ["gunicorn", "download.server:app", "--bind", "0.0.0.0:8080"]
