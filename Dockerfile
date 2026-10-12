# SheetTwin in one image: the Next.js UI is built to static files and served by the FastAPI backend (`sheetir serve`).
#   docker compose up --build        then open http://localhost:8765

# ---- 1. UI: static export, copied by `npm run build` into sheetir/web/static/app ---------------------------------
FROM node:22-bookworm-slim AS ui
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 2. Backend: Python dependencies pinned by uv.lock, plus the UI from stage 1 ---------------------------------
FROM python:3.13-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first, so editing SheetIR's code does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY sheetir/ ./sheetir/
RUN uv sync --frozen --no-dev
COPY --from=ui /src/sheetir/web/static/app ./sheetir/web/static/app

RUN useradd --create-home --uid 1000 sheettwin && mkdir /data && chown sheettwin /data
USER sheettwin
ENV PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/workbooks', timeout=4)"
CMD ["sheetir", "serve", "--host", "0.0.0.0", "--port", "8765", "--data", "/data"]
