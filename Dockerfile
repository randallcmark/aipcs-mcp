FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

WORKDIR /opt/aipcs-mcp
COPY . .
RUN python -m pip install --no-cache-dir ".[postgresql]" \
    && useradd --create-home --shell /usr/sbin/nologin aipcs

USER aipcs
ENTRYPOINT ["aipcs"]
CMD ["serve", "--config", "/etc/aipcs/config.toml"]
