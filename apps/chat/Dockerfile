# syntax=docker/dockerfile:1

# ---- Stage 1: dependencies --------------------------------------------------
# Only the manifests are copied, so this layer is reused until the lockfile moves.
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ---- Stage 2: build ---------------------------------------------------------
FROM node:20-alpine AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- Stage 3: runtime -------------------------------------------------------
# Ships only the standalone bundle: no npm cache, no dev dependencies, no source.
FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN addgroup -S nexus && adduser -S nexus -G nexus
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nexus:nexus /app/.next/standalone ./
COPY --from=builder --chown=nexus:nexus /app/.next/static ./.next/static
USER nexus
EXPOSE 3000
CMD ["node", "server.js"]
