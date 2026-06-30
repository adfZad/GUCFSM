FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends supervisor \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY supervisord.conf /etc/supervisor/conf.d/bots.conf

RUN mkdir -p /data/photos /logs

WORKDIR /app

CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
