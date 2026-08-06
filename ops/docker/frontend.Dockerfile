# Neryva Agent Studio - admin console image (Vite -> nginx, P0-12)
FROM node:22-alpine AS build
RUN corepack enable
WORKDIR /app
COPY package.json pnpm-workspace.yaml ./
COPY frontend ./frontend
RUN pnpm install --no-frozen-lockfile \
    && pnpm --filter neryva-frontend build

FROM nginx:1.27-alpine
COPY --from=build /app/frontend/dist /usr/share/nginx/html
EXPOSE 80
