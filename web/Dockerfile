# Build context: the repository root (see infra/compose.yaml).
FROM node:24 AS build
RUN npm install --global pnpm@11.21.0
WORKDIR /repo/web
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM caddy:2
COPY --from=build /repo/web/dist /srv
COPY infra/caddy/Caddyfile /etc/caddy/Caddyfile
