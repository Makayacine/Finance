# Build from the project root, so the shared credit_risk package is in the context:
#   docker build -f docker-data-validity/Dockerfile -t sba-data-validity .
# The base image comes from ECR Public's mirror of the Docker official image: the same bytes as
# python:3.12-slim, without Docker Hub's anonymous pull limit, which a CodeBuild or ECS host
# behind a shared NAT address reaches quickly.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
COPY docker-data-validity/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY credit_risk/ credit_risk/
COPY docker-data-validity/data-validity.py .

ENTRYPOINT ["python", "data-validity.py"]
