# syntax=docker.io/docker/dockerfile:1.7.0@sha256:dbbd5e059e8a07ff7ea6233b213b36aa516b4c53c645f1817a4dd18b83cbea56
FROM docker.io/library/rust:1.94.0-bookworm@sha256:365468470075493dc4583f47387001854321c5a8583ea9604b297e67f01c5a4f AS rust-toolchain
FROM quay.io/pypa/manylinux2014_x86_64@sha256:95440e0e72dd3a81dc8d2cf59a84d57af661456620f5bc821ff92048d0e54ff9 AS noether-wheel
ARG SOURCE_DATE_EPOCH
ENV PATH=/usr/local/cargo/bin:/usr/local/bin:/opt/python/cp310-cp310/bin:/opt/clang/bin:/opt/rh/devtoolset-10/root/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CC=/opt/rh/devtoolset-10/root/usr/bin/cc \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    UV_PYTHON_DOWNLOADS=never
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
ADD --checksum=sha256:74947fe2c03315cf07e82ab3acc703eddef01aba4d5232a98e4c6825ec116131 \
    https://github.com/astral-sh/uv/releases/download/0.11.16/uv-x86_64-unknown-linux-gnu.tar.gz /tmp/uv.tar.gz
RUN tar -xzf /tmp/uv.tar.gz -C /tmp \
    && install -m 0755 /tmp/uv-x86_64-unknown-linux-gnu/uv /usr/local/bin/uv \
    && test "$(command -v cc)" = "$CC" \
    && test "$(uv --version | awk '{print $2}')" = "0.11.16" \
    && test "$(rustc --version | awk '{print $2}')" = "1.94.0" \
    && rm -rf /tmp/uv.tar.gz /tmp/uv-x86_64-unknown-linux-gnu
COPY --from=noether-source . /work/noether
WORKDIR /work/noether
RUN test "$SOURCE_DATE_EPOCH" -gt 0 \
    && python -c 'import json; p=json.load(open("reasoning-source-pin.json")); assert p == {"commit":"eebc33d4f37ed47af83c88dbc9f8886b254f72c6","version":"0.3.2"}' \
    && test "$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -n 1)" = "0.3.2" \
    && test "$(sed -n 's/^channel = "\([^"]*\)"/\1/p' rust-toolchain.toml)" = "1.94.0" \
    && test -f Cargo.lock && test -f uv.lock \
    && uv venv --python /opt/python/cp310-cp310/bin/python .venv \
    && VIRTUAL_ENV=/work/noether/.venv uv sync --active --python /opt/python/cp310-cp310/bin/python \
         --locked --inexact --no-install-project --group dev \
    && RUN_NATIVE_TESTS=0 scripts/install-flint.sh \
    && FLINT_PREFIX=/work/noether/.deps/flint make wheel WHEEL_DIR=/wheelhouse \
         MATURIN_WHEEL_ARGS='--compatibility manylinux2014 --locked' \
    && test "$(find /wheelhouse -maxdepth 1 -type f -name 'tertius_noether-*.whl' | wc -l)" -eq 1 \
    && scripts/check-wheel.sh "$(find /wheelhouse -maxdepth 1 -type f -name 'tertius_noether-*.whl')" \
    && du -sh /work/noether /usr/local/cargo \
    && rm -rf /work/noether/target /work/noether/.venv /work/noether/.deps \
         /usr/local/cargo/registry /usr/local/cargo/git /root/.cache/uv \
    && du -sh /work/noether /usr/local/cargo /wheelhouse

FROM docker.io/sagemath/sagemath:10.6@sha256:19995db6194f4a4bab18ce9a88556fd15b9ed5e916b4504fefe618a7796ddbdb
ARG SOURCE_REVISION
ARG SOURCE_DATE_EPOCH
ARG BUILD_TIMESTAMP
USER root
ADD --checksum=sha256:fb49c6b174600cd2051e37ba3f7330f8acf06dd00bce609bab6611387fdb37bf \
    https://julialang-s3.julialang.org/bin/linux/x64/1.10/julia-1.10.11-linux-x86_64.tar.gz /tmp/reasoning-julia.tar.gz
ADD --checksum=sha256:8f2ce2aa6de5ede5847a3e9517a930ede3d6954444f44535155da3250ac7f2fc \
    https://github.com/leanprover/lean4/releases/download/v4.24.0/lean-4.24.0-linux.zip /tmp/reasoning-lean.zip
RUN test "$SOURCE_DATE_EPOCH" -gt 0 \
    && mkdir -p /opt/julia /opt/reasoning-lean-extract \
    && tar -xzf /tmp/reasoning-julia.tar.gz -C /opt/julia --strip-components=1 \
    && sage -python -m zipfile -e /tmp/reasoning-lean.zip /opt/reasoning-lean-extract \
    && mv /opt/reasoning-lean-extract/lean-4.24.0-linux /opt/lean \
    && chmod 0755 /opt/lean/bin/* \
    && rm /tmp/reasoning-julia.tar.gz /tmp/reasoning-lean.zip \
    && rmdir /opt/reasoning-lean-extract \
    && useradd --uid 10001 --user-group --no-create-home --no-log-init --home-dir /tmp --shell /usr/sbin/nologin reasoning-program \
    && useradd --uid 10002 --user-group --no-create-home --no-log-init --home-dir / --shell /usr/sbin/nologin reasoning-supervisor \
    && chage --lastday "$((SOURCE_DATE_EPOCH / 86400))" reasoning-program \
    && chage --lastday "$((SOURCE_DATE_EPOCH / 86400))" reasoning-supervisor \
    && rm -f /etc/passwd- /etc/shadow- /etc/group- /etc/gshadow- \
    && chmod a+rx /home/sage \
    && mkdir -p /work /etc/reasoning /usr/share/reasoning/licenses
ENV PATH=/opt/reasoning/bin:/opt/julia/bin:/opt/lean/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DOT_SAGE=/tmp/sage \
    JULIA_DEPOT_PATH=/tmp/julia \
    XDG_CACHE_HOME=/tmp/xdg \
    PYTHONDONTWRITEBYTECODE=1 \
    REASONING_OUTPUT_DIR=/work/out
COPY --from=noether-wheel /wheelhouse /tmp/noether-wheelhouse
RUN wheel="$(find /tmp/noether-wheelhouse -maxdepth 1 -type f -name 'tertius_noether-*.whl')" \
    && test -n "$wheel" \
    && sage -python -m pip install --no-deps --no-cache-dir "$wheel" \
    && mkdir -p /usr/share/reasoning/provenance \
    && sha256sum "$wheel" | awk '{print $1}' > /usr/share/reasoning/provenance/noether-wheel.sha256 \
    && sage -python -c 'import noether; assert noether.__version__ == "0.3.2"; noether.capabilities()' \
    && rm -rf /tmp/noether-wheelhouse
COPY images/research-cpu/runtime/ /opt/reasoning/bin/
COPY src/reasoning_ledger/agents/noether_worker.py /opt/reasoning/noether_worker.py
COPY --from=rust-toolchain /usr/local/rustup/toolchains/1.94.0-x86_64-unknown-linux-gnu/share/doc/rust/COPYRIGHT-library.html /usr/share/reasoning/licenses/rust-1.94.0/COPYRIGHT-library.html
COPY --from=rust-toolchain /usr/local/rustup/toolchains/1.94.0-x86_64-unknown-linux-gnu/share/doc/rust/licenses/MIT.txt /usr/share/reasoning/licenses/rust-1.94.0/LICENSE-MIT
COPY --from=rust-toolchain /usr/local/rustup/toolchains/1.94.0-x86_64-unknown-linux-gnu/share/doc/rust/licenses/Apache-2.0.txt /usr/share/reasoning/licenses/rust-1.94.0/LICENSE-APACHE
COPY images/research-cpu/metadata.py /opt/reasoning/metadata.py
COPY images/research-cpu/inputs.json images/research-cpu/THIRD_PARTY_NOTICES.txt /usr/share/reasoning/
COPY LICENSE NOTICE /usr/share/reasoning/licenses/
RUN chmod 0755 /opt/reasoning/bin/* \
    && sage -python /opt/reasoning/metadata.py \
         --lock /usr/share/reasoning/inputs.json \
         --source-revision "$SOURCE_REVISION" --build-timestamp "$BUILD_TIMESTAMP" \
         --capabilities /etc/reasoning/capabilities.json \
         --sbom /usr/share/reasoning/sbom.spdx.json \
    && rm -rf /tmp/sage /tmp/julia /tmp/xdg
LABEL org.opencontainers.image.title="Reasoning research-cpu" \
      org.opencontainers.image.description="Pinned CPU mathematics runtimes for isolated Reasoning workers" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.reasoning.executor.contract="1.0.0-alpha.1" \
      org.reasoning.runtime.network-default="denied-by-worker"
USER 10002:10002
WORKDIR /
ENTRYPOINT []
CMD ["/bin/sleep", "60"]
