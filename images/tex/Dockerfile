# syntax=docker.io/docker/dockerfile:1.7.0@sha256:dbbd5e059e8a07ff7ea6233b213b36aa516b4c53c645f1817a4dd18b83cbea56
FROM docker.io/library/ubuntu:jammy-20260930@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956
ARG SOURCE_REVISION
ARG SOURCE_DATE_EPOCH
ARG BUILD_TIMESTAMP
USER root
ADD --checksum=sha256:6e8cdcc8c86103acd4fc14649eac62ff2037108389074a7b167567af33c32245 \
    https://snapshot.ubuntu.com/ubuntu/20261006T000000Z/pool/main/c/ca-certificates/ca-certificates_20260601~22.04.1_all.deb /tmp/reasoning-ca-certificates.deb
# Only the signed, immutable Ubuntu snapshot is consulted, never the base image's live
# mirrors. HTTPS is checked against the locked certificate package's roots, which are
# extracted for this step only and are not installed in the image.
RUN test "$SOURCE_DATE_EPOCH" -gt 0 \
    && mkdir /tmp/reasoning-ca \
    && dpkg-deb -x /tmp/reasoning-ca-certificates.deb /tmp/reasoning-ca \
    && cat /tmp/reasoning-ca/usr/share/ca-certificates/mozilla/*.crt > /tmp/reasoning-ca-roots.crt \
    && printf 'deb https://snapshot.ubuntu.com/ubuntu/20261006T000000Z %s main universe\n' jammy jammy-updates jammy-security \
         > /tmp/reasoning-build.sources.list \
    && set -- -o Dir::Etc::sourcelist=/tmp/reasoning-build.sources.list -o Dir::Etc::sourceparts=- \
         -o Acquire::https::CaInfo=/tmp/reasoning-ca-roots.crt -o Acquire::Check-Valid-Until=false -o APT::Update::Error-Mode=any \
    && apt-get "$@" update \
    && DEBIAN_FRONTEND=noninteractive FORCE_SOURCE_DATE=1 apt-get "$@" install -y --no-install-recommends \
         biber=2.17-2 \
         cm-super-minimal=0.3.4-17 \
         latexmk=1:4.76-1 \
         lmodern=2.004.5-6.1 \
         python3=3.10.6-1~22.04.1 \
         texlive-bibtex-extra=2021.20220204-1 \
         texlive-binaries=2021.20210626.59705-1ubuntu0.3 \
         texlive-fonts-recommended=2021.20220204-1 \
         texlive-latex-base=2021.20220204-1 \
         texlive-latex-extra=2021.20220204-1 \
         texlive-latex-recommended=2021.20220204-1 \
         texlive-pictures=2021.20220204-1 \
         texlive-science=2021.20220204-1 \
    && apt-get "$@" clean \
    && rm -rf /var/lib/apt/lists/* /tmp/reasoning-ca /tmp/reasoning-ca-roots.crt /tmp/reasoning-ca-certificates.deb \
         /tmp/reasoning-build.sources.list /var/log/apt /var/log/dpkg.log /var/log/alternatives.log \
         /var/cache/debconf/*-old /var/cache/ldconfig/aux-cache \
    && useradd --uid 10001 --user-group --no-create-home --no-log-init --home-dir /tmp --shell /usr/sbin/nologin reasoning-program \
    && useradd --uid 10002 --user-group --no-create-home --no-log-init --home-dir / --shell /usr/sbin/nologin reasoning-supervisor \
    && chage --lastday "$((SOURCE_DATE_EPOCH / 86400))" reasoning-program \
    && chage --lastday "$((SOURCE_DATE_EPOCH / 86400))" reasoning-supervisor \
    && rm -f /etc/passwd- /etc/shadow- /etc/group- /etc/gshadow- \
    && mkdir -p /work /etc/reasoning /opt/reasoning /usr/share/reasoning/licenses
# The worker sets the same job environment for every image; inspection requires it
# here as well. TeX's writable caches live on the job's private /tmp.
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DOT_SAGE=/tmp/sage \
    JULIA_DEPOT_PATH=/tmp/julia \
    XDG_CACHE_HOME=/tmp/xdg \
    PYTHONDONTWRITEBYTECODE=1 \
    REASONING_OUTPUT_DIR=/work/out \
    TEXMFVAR=/tmp/texmf-var \
    TEXMFCONFIG=/tmp/texmf-config \
    LANG=C.UTF-8
COPY images/tex/metadata.py /opt/reasoning/metadata.py
COPY images/tex/inputs.json images/tex/THIRD_PARTY_NOTICES.txt /usr/share/reasoning/
COPY LICENSE NOTICE /usr/share/reasoning/licenses/
RUN python3 /opt/reasoning/metadata.py \
         --lock /usr/share/reasoning/inputs.json \
         --source-revision "$SOURCE_REVISION" --build-timestamp "$BUILD_TIMESTAMP" \
         --capabilities /etc/reasoning/capabilities.json \
         --sbom /usr/share/reasoning/sbom.spdx.json \
    && rm -rf /tmp/texmf-var /tmp/texmf-config /tmp/xdg
LABEL org.opencontainers.image.title="Reasoning TeX" \
      org.opencontainers.image.description="Pinned offline TeX Live for compiling LaTeX to PDF in isolated Reasoning workers" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.reasoning.executor.contract="1.0.0-alpha.1" \
      org.reasoning.runtime.network-default="denied-by-worker"
USER 10002:10002
WORKDIR /
ENTRYPOINT []
CMD ["/bin/sleep", "60"]
