# syntax=docker/dockerfile:1
#
# Interview Assistant web app (Next.js) image (M-12, D-79).
#
#   docker build -f apps/web/Dockerfile --build-arg API_INTERNAL_URL=http://api:8000 -t interview-assistant-web apps/web
#
# IMPORTANT: Next.js resolves the /api/* rewrite at BUILD time, so API_INTERNAL_URL is a build
# argument: rebuild the image when the API address changes. No secret belongs in this image.
ARG NODE_IMAGE=node:22-bookworm-slim

FROM ${NODE_IMAGE} AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG API_INTERNAL_URL=http://api:8000
ENV API_INTERNAL_URL=${API_INTERNAL_URL} NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM ${NODE_IMAGE} AS runtime
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000
WORKDIR /app
# Only what `next start` needs; the unprivileged "node" user (uid 1000) owns nothing writable.
COPY --from=build --chown=root:root /app/package.json /app/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build --chown=root:root /app/.next ./.next
COPY --from=build --chown=root:root /app/public ./public
COPY --from=build --chown=root:root /app/next.config.ts ./next.config.ts
COPY --from=build --chown=root:root /app/lib ./lib
USER node
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/login').then(r => process.exit(r.status < 500 ? 0 : 1)).catch(() => process.exit(1))"]
CMD ["npx", "next", "start", "--hostname", "0.0.0.0"]
