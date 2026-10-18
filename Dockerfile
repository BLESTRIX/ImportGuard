FROM ghcr.io/astral-sh/uv:0.12.10 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /uvx /bin/
WORKDIR /srv/importguard
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev && .venv/bin/python -c "import fastapi, sqlalchemy, pydantic, celery, boto3"
COPY --chown=10001:10001 . .
RUN useradd --create-home --uid 10001 importguard && getent passwd importguard
USER 10001:10001
ENV PATH="/srv/importguard/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
