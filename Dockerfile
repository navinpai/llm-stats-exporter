FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-trixie
RUN groupadd -r -g 65532 exporter && useradd -r -u 65532 -g exporter exporter
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
# Numeric USER so Kubernetes runAsNonRoot can verify the image is non-root.
USER 65532
EXPOSE 9184
CMD ["llm-stats-exporter"]
