FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# The bundle and nginx share one build id: nginx sends it as X-SMT-Build on every /api/
# response, so a tab running an older bundle sees the mismatch and offers a reload.
ARG SMT_BUILD_ID=
RUN BUILD_ID="${SMT_BUILD_ID:-$(date -u +%Y%m%d%H%M%S)}" \
    && printf 'set $smt_build "%s";\n' "$BUILD_ID" > /app/build-id.conf \
    && VITE_BUILD_ID="$BUILD_ID" npm run build

FROM nginx:1.27-alpine
RUN apk add --no-cache openssl
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY nginx/app.conf nginx/proxy-common.conf nginx/proxy-api.conf nginx/ca-cert.conf \
     nginx/security-headers.conf /etc/nginx/smt/
COPY --chmod=755 nginx/40-smtrading-tls.sh /docker-entrypoint.d/40-smtrading-tls.sh
COPY --from=build /app/build-id.conf /etc/nginx/smt/build-id.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80 443 8080
