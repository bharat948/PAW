# Hermes runtime wrapper image for PAW-002 (spike S0). No official image exists, so we build one.
# Reproducibility and supply-chain controls:
#   - the vendor install script is downloaded and must match INSTALL_SH_SHA256 (the version we reviewed),
#     so any upstream change fails the build and forces a re-review
#   - Hermes itself is pinned to an exact commit (HERMES_COMMIT), not "latest"
#   - runs as a non-root user; no secrets are baked in (see entrypoint.sh)
FROM debian:12-slim

ARG INSTALL_SH_SHA256
ARG HERMES_COMMIT
RUN test -n "$INSTALL_SH_SHA256" && test -n "$HERMES_COMMIT"

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl git ca-certificates libatomic1 xz-utils procps \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10002 hermes
USER hermes
WORKDIR /home/hermes
ENV HERMES_HOME=/home/hermes/.hermes HOME=/home/hermes

RUN curl -fsSL -o /tmp/install.sh https://hermes-agent.nousresearch.com/install.sh \
 && echo "${INSTALL_SH_SHA256}  /tmp/install.sh" | sha256sum -c - \
 && bash /tmp/install.sh --non-interactive --skip-browser --skip-computer-use \
      --commit "${HERMES_COMMIT}" --hermes-home "${HERMES_HOME}" \
 && rm -f /tmp/install.sh

ENV PATH="/home/hermes/.local/bin:${PATH}"
COPY --chown=hermes:hermes plugins/ /opt/paw-plugins/
COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["hermes", "gateway"]
