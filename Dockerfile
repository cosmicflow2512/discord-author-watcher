FROM tyrrrz/discordchatexporter:2.48
USER root
# Python aus demselben Alpine-Repository wie das Basisimage: keine fremden musl/libssl-Abhängigkeiten.
RUN apk add --no-cache python3 su-exec ca-certificates \
 && mkdir -p /data /watcher
WORKDIR /watcher
COPY --chmod=0644 watcher.py .
COPY --chmod=0755 entrypoint.sh .
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data TZ=Europe/Berlin PUID=99 PGID=100
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python3", "/watcher/watcher.py", "--healthcheck"]
ENTRYPOINT ["/watcher/entrypoint.sh"]
