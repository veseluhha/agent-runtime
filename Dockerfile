FROM docker:28.0.1-cli AS cli
FROM python:3.12.10-alpine3.21 AS base
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
FROM base AS app
COPY runtime runtime
FROM base AS gateway
RUN pip install --no-cache-dir PyYAML==6.0.2 fastjsonschema==2.21.1
COPY runtime runtime
COPY baf baf
USER 65534:65534
CMD ["python", "-m", "runtime.gateway"]
FROM app AS manager
COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker
CMD ["python", "-m", "runtime.manager"]
