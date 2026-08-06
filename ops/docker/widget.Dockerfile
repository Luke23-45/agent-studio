# Neryva Agent Studio - embeddable widget image (Vite -> nginx, P0-12)
FROM node:22-alpine AS build
RUN corepack enable
WORKDIR /app
COPY package.json pnpm-workspace.yaml ./
COPY widget ./widget
RUN pnpm install --no-frozen-lockfile \
    && pnpm --filter neryva-widget build

FROM nginx:1.27-alpine
COPY --from=build /app/widget/dist /usr/share/nginx/html
EXPOSE 80
