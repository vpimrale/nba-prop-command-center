# NOT BUILT OR PUBLISHED AUTOMATICALLY. Optional container for a machine you control.
#   docker compose -f deploy/docker-compose.yml up -d --build
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY nba_edge ./nba_edge
RUN pip install --no-cache-dir --no-deps . && useradd -m -u 10001 edge && mkdir -p /data && chown edge /data
USER edge
ENV NBA_EDGE_DATABASE_URL=sqlite:////data/nba_edge.sqlite3 NBA_EDGE_BACKUP_DIR=/data/backups
# Healthy = the scheduler ticked recently and nothing is critical (exit 2 = critical).
HEALTHCHECK --interval=10m --timeout=60s --start-period=10m CMD python -m nba_edge health > /dev/null; test $? -lt 2
CMD ["python", "-m", "nba_edge", "run-scheduler"]
