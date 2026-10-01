FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config
COPY site ./site
ENV RADIO_DATA_DIR=/data RADIO_CONFIG_DIR=/app/config RADIO_SITE_DIR=/site
EXPOSE 8081
CMD ["radio","sidecar"]

