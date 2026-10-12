FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY argos/ ./argos/
RUN python -m pip install --no-cache-dir '.[console,mavlink]' \
    && useradd --uid 10001 --create-home argos

COPY examples/demo-flight/ ./examples/demo-flight/
COPY examples/verify_demo_flight.py ./examples/verify_demo_flight.py
RUN python examples/verify_demo_flight.py

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/demo-config', timeout=3).close()"

ENTRYPOINT ["python", "-m", "argos.console"]
CMD ["--demo", "--host", "0.0.0.0", "--port", "8080", "--public-host", "localhost", "--recordings-dir", "/app/examples/demo-flight"]
