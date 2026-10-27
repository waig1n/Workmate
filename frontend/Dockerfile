FROM node:22-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json ./
# ci = reproducible install from the lockfile (^ version ranges in
# package.json otherwise float between builds).
RUN npm ci --no-audit --no-fund

COPY . .
# The dev proxy rewrites /api/* server-side, so PRODUCT_API_URL is a
# build-time constant pointing at the gateway service name.
ARG PRODUCT_API_URL=http://gateway:8080
ENV PRODUCT_API_URL=$PRODUCT_API_URL
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine AS runner

WORKDIR /app
ARG PRODUCT_API_URL=http://gateway:8080
ENV PRODUCT_API_URL=$PRODUCT_API_URL
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=builder /app/LICENSE /app/NOTICE /usr/share/doc/workmate/
COPY --from=builder /app/package.json ./
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public

EXPOSE 3000

CMD ["npm", "run", "start"]
