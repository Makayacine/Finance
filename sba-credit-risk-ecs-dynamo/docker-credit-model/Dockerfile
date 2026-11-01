# Build from the project root, so the shared credit_risk package is in the context:
#   docker build -f docker-credit-model/Dockerfile -t sba-credit-model .
# Same base as the validator, for the same reason; see docker-data-validity/Dockerfile.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
COPY docker-credit-model/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY credit_risk/ credit_risk/
COPY docker-credit-model/credit-model.py .

ENTRYPOINT ["python", "credit-model.py"]
