FROM node:24.22.1-bookworm-slim AS node
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM python:3.12.14-slim-bookworm
WORKDIR /app
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /app/node_modules /app/node_modules
COPY locks/acceptance.txt locks/acceptance.txt
RUN python -m pip install --no-cache-dir --require-hashes -r locks/acceptance.txt && \
    node node_modules/playwright/cli.js install --with-deps msedge
COPY . .
