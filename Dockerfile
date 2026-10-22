FROM node:22-alpine AS dashboard
WORKDIR /web
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web ./
RUN npm run build

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY apps ./apps

RUN pip install --no-cache-dir -e ".[api,temporal,llm,parsing]"

COPY --from=dashboard /web/dist ./apps/web/dist

CMD ["python", "apps/worker/main.py"]
