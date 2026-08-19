# Shared base image for all ApprovalFlow services.
# Each service Dockerfile does: FROM shared-base
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi[standard] uvicorn dapr-ext-fastapi httpx

COPY pyproject.toml approvalflow/__init__.py ./
COPY approvalflow/ ./approvalflow/
RUN pip install --no-cache-dir -e .
