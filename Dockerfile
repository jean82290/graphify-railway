FROM python:3.12-slim

# git is needed to clone and refresh the repository being mapped.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Pinned Graphify release (it ships almost daily). Change it here or with a GRAPHIFY_VERSION build arg.
ARG GRAPHIFY_VERSION=0.9.97
RUN pip install --no-cache-dir "graphifyy[mcp]==${GRAPHIFY_VERSION}"

WORKDIR /app
COPY launch.py ./

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8080

EXPOSE 8080
CMD ["python", "/app/launch.py"]
