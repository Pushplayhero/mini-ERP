# syntax=docker/dockerfile:1

# --- builder ---------------------------------------------------------------
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /build

COPY pyproject.toml ./
COPY README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Resolve + install only production dependencies into a project-local venv
# (no dev/test tooling in the runtime image).
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python .

# --- runtime -----------------------------------------------------------
FROM python:3.12-slim AS runtime

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY --from=builder /build/app ./app
COPY --from=builder /build/alembic ./alembic
COPY --from=builder /build/alembic.ini ./alembic.ini

USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
