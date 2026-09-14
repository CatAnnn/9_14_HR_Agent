FROM node:22-alpine AS build
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    http_proxy=${HTTP_PROXY} \
    https_proxy=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY} \
    no_proxy=${NO_PROXY}
WORKDIR /app/frontend
COPY frontend/package*.json frontend/tsconfig.json frontend/vite.config.ts frontend/postcss.config.mjs frontend/index.html ./
COPY frontend/src ./src
COPY Ebook /app/Ebook
COPY data/frontend /app/data/frontend
RUN npm ci --include=dev --registry=https://registry.npmjs.org --replace-registry-host=always && npm run build

FROM nginx:1.27-alpine
COPY frontend/nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/frontend/dist /usr/share/nginx/html
EXPOSE 7070 7443
CMD ["nginx", "-g", "daemon off;"]
