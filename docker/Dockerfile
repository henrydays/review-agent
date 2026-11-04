# Benchmark image: the pr-review skill on OpenCode, wrapped to satisfy the ReviewBench agent contract.
#
# Credentials arrive at run time and are never baked in. Pick one, and declare it in the manifest:
#   OPENCODE_API_KEY   for opencode-go/* or opencode/* models. Egress: opencode.ai
#   DEEPSEEK_API_KEY   for deepseek/* models.                  Egress: api.deepseek.com
#
# Configuration, from the manifest's `configuration` block:
#   RB_CONFIG_MODEL      for example deepseek/deepseek-flash or opencode-go/deepseek-v4.1-flash
#   RB_CONFIG_PROFILE    exhaustive (default), balanced or strict
#   RB_CONFIG_DEADLINE   seconds for one PR; the default scales with the size of the PR
#
# Build from the repository root:
#   docker build --platform linux/amd64 -f docker/Dockerfile -t pr-review:dev .
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates python3 ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --no-audit --no-fund @opencode/cli@2.2.0 \
    && npm cache clean --force

WORKDIR /agent
COPY skill/pr-review ./skill/pr-review
COPY docker/entrypoint.py ./entrypoint.py

# Nothing should phone home: the benchmark only allows declared hosts, and a blocked request can stall startup.
ENV OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_LSP_DOWNLOAD=1 \
    OPENCODE_DISABLE_DEFAULT_PLUGINS=1 \
    OPENCODE_DISABLE_SHARE=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python3", "/agent/entrypoint.py"]
