FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
RUN apk add --no-cache openssl
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY nginx/app.conf nginx/proxy-common.conf nginx/proxy-api.conf nginx/ca-cert.conf \
     nginx/security-headers.conf /etc/nginx/smt/
COPY --chmod=755 nginx/40-smtrading-tls.sh /docker-entrypoint.d/40-smtrading-tls.sh
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80 443 8080
