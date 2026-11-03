# Build from the project root, so the bond_analytics package, the loader and the fetcher are in
# the context: the daily engine needs the first two, the backfill all three.
#   docker build -f ecs/Dockerfile -t sovereign-curves-engine .
FROM public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app
COPY ecs/requirements.txt ecs/requirements.txt
RUN pip install --no-cache-dir -r ecs/requirements.txt

COPY bond_analytics/ bond_analytics/
COPY glue-pythonshell/load.py glue-pythonshell/load.py
COPY lambda-functions/fetch.py lambda-functions/fetch.py
COPY ecs/app.py ecs/app.py

RUN useradd --uid 10001 --no-create-home engine
USER engine

ENTRYPOINT ["python", "ecs/app.py"]
CMD ["--mode", "daily"]
