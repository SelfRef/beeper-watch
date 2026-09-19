# beeper-watch — rules that turn bridged Beeper events into n8n webhook calls.
#
# Beeper has no event API: no webhooks, no gateway, and its Desktop API is
# request/response only. The self-hosted bridges are therefore patched to
# report every event they handle (the beeper-watch patch in
# beeper-bridge-manager-docker), and this is what they report to.
#
# Everything lives in one SQLite file on /data: the rules, a short buffer of
# recent events so a rule can be dry-run against real traffic before it is
# enabled, and the delivery outbox that makes a match survive n8n restarting.
# The stack's shared Postgres was the obvious alternative and was rejected on
# purpose — this service has to be up whenever the bridges are up.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    BEEPER_WATCH_DB=/data/beeper-watch.db

VOLUME /data
EXPOSE 8080

# One worker on purpose: the SQLite writer, the delivery outbox and the MCP
# session state are all process-local, and the measured load is about 150
# events a day.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
