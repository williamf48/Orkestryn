FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=10000 AR_STATE_ROOT=/var/data/agent-rehearsal
WORKDIR /opt/orkestryn
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home rehearsal \
    && mkdir -p /var/data/agent-rehearsal \
    && chown -R rehearsal:rehearsal /var/data/agent-rehearsal
COPY . .
USER rehearsal
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','10000')+'/healthz',timeout=3).read()"
CMD ["python", "app/hosted_server.py"]
