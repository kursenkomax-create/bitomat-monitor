FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY bitomat_monitor.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "bitomat_monitor.py"]
