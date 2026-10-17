# Static judge website only (read-only documentation). Build context = this folder. Not built, not deployed until approved.
FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY judges/ /usr/share/nginx/html/
EXPOSE 8080
