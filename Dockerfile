FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite database lives on a named volume provided by docker-compose.
ENV CUTTING_DB=/data/cutting.db
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 5000

# Single worker keeps the in-process setup simple; writes are still guarded
# by BEGIN IMMEDIATE transactions in storage.register_cut.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "60", "app:app"]
