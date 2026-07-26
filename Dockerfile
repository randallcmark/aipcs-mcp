FROM python:3.14-slim

WORKDIR /opt/aipcs-mcp
COPY . .
RUN python -m pip install --no-cache-dir ".[postgresql]" \
    && useradd --create-home --shell /usr/sbin/nologin aipcs

USER aipcs
ENTRYPOINT ["aipcs"]
CMD ["serve", "--config", "/etc/aipcs/config.toml"]
