FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS runtime-builder

ARG PYTHON_VERSION=3.14.6
ARG PYTHON_SHA256=74d0d71d0600e477651a077101d6e62d1e2e69b8e992ba18c993dd643b7ba222
ARG SQLITE_AUTOCONF=3530400
ARG SQLITE_SHA3_256=454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential ca-certificates curl libbz2-dev libffi-dev libgdbm-dev \
        liblzma-dev libncursesw5-dev libreadline-dev libssl-dev uuid-dev zlib1g-dev \
    && curl --fail --location --silent --show-error \
        "https://sqlite.org/2026/sqlite-autoconf-${SQLITE_AUTOCONF}.tar.gz" \
        --output /tmp/sqlite.tar.gz \
    && SQLITE_SHA3_256="$SQLITE_SHA3_256" python -c '\
import hashlib, os, pathlib, sys; \
actual = hashlib.sha3_256(pathlib.Path("/tmp/sqlite.tar.gz").read_bytes()).hexdigest(); \
sys.exit(0 if actual == os.environ["SQLITE_SHA3_256"] else 1)' \
    && tar --extract --gzip --file /tmp/sqlite.tar.gz --directory /tmp \
    && cd "/tmp/sqlite-autoconf-${SQLITE_AUTOCONF}" \
    && ./configure --prefix=/opt/sqlite --disable-static \
    && make --jobs "$(nproc)" \
    && make install \
    && rm --recursive --force /tmp/sqlite.tar.gz "/tmp/sqlite-autoconf-${SQLITE_AUTOCONF}"

RUN curl --fail --location --silent --show-error \
        "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz" \
        --output /tmp/python.tgz \
    && echo "${PYTHON_SHA256}  /tmp/python.tgz" | sha256sum --check --status \
    && tar --extract --gzip --file /tmp/python.tgz --directory /tmp \
    && cd "/tmp/Python-${PYTHON_VERSION}" \
    && CPPFLAGS="-I/opt/sqlite/include" \
       LDFLAGS="-L/opt/sqlite/lib -Wl,-rpath,/opt/sqlite/lib" \
       PKG_CONFIG_PATH="/opt/sqlite/lib/pkgconfig" \
       ./configure --prefix=/opt/python --with-ensurepip=install --enable-loadable-sqlite-extensions \
    && make --jobs "$(nproc)" \
    && make install \
    && ln --symbolic python3.14 /opt/python/bin/python \
    && LD_LIBRARY_PATH=/opt/sqlite/lib /opt/python/bin/python -c \
        'import sqlite3, sys; print(sqlite3.sqlite_version); sys.exit(0 if sqlite3.sqlite_version_info >= (3, 51, 3) else 1)' \
    && rm --recursive --force /tmp/python.tgz "/tmp/Python-${PYTHON_VERSION}"

FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

COPY --from=runtime-builder /opt/python /opt/python
COPY --from=runtime-builder /opt/sqlite /opt/sqlite

ENV PATH="/opt/python/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/sqlite/lib"

WORKDIR /opt/aipcs-mcp
COPY . .
RUN python -m pip install --no-cache-dir ".[postgresql]" \
    && useradd --create-home --home-dir /app --shell /usr/sbin/nologin --uid 10001 aipcs \
    && install --directory --owner=aipcs --group=aipcs --mode=0700 /app/.local \
    && install --directory --owner=aipcs --group=aipcs --mode=0700 /app/.local/share \
    && install --directory --owner=aipcs --group=aipcs --mode=0700 /app/.local/share/aipcs

USER aipcs
ENTRYPOINT ["aipcs"]
CMD ["serve", "--profile", "sqlite", "--principal-id", "aipcs"]

VOLUME ["/app/.local/share/aipcs"]
