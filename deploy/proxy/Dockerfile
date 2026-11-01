# Built SPA + same-origin proxy. Build context is the repository root.
FROM node:20.20-slim AS build
WORKDIR /app
COPY paw-web/package.json paw-web/package-lock.json ./
RUN npm ci
COPY paw-web/ .
ENV VITE_API_BASE_URL=/api
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY deploy/proxy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
