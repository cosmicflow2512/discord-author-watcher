FROM python:3.12-alpine3.24 AS python
FROM tyrrrz/discordchatexporter:2.48
USER root
COPY --from=python /usr/local /usr/local
COPY --from=python /usr/lib/libsqlite3.so* /usr/lib/
RUN mkdir -p /data /watcher
WORKDIR /watcher
COPY watcher.py .
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data TZ=Europe/Berlin
VOLUME ["/data"]
ENTRYPOINT ["python3", "/watcher/watcher.py"]
