# Build a wheel of the service and its dependencies, then install the wheels in a
# clean slim image that runs as an unprivileged user.
FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLAGS_DATABASE=/data/flags.db
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin flags \
    && mkdir /data \
    && chown flags /data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl && rm -rf /wheels
USER flags
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
# FLAGS_ADMIN_KEY must be provided at run time.
CMD ["flagservice-server", "--host", "0.0.0.0", "--port", "8000"]
