FROM node:22.18.0-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git util-linux python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
ENV GITNEXUS_SKIP_OPTIONAL_GRAMMARS=1 \
    SCARF_ANALYTICS=false \
    HOME=/state/home \
    GITNEXUS_HOME=/state/home/.gitnexus \
    GITNEXUS_STORAGE_PATH=/state/index \
    GITNEXUS_SHARED_STORE=off \
    GITNEXUS_CONTENT_RETENTION=none
RUN npm install --global gitnexus@1.6.12
WORKDIR /opt/codegraph
COPY apps/codegraph/entrypoint.sh apps/codegraph/prepare.mjs ./
ENTRYPOINT ["sh", "/opt/codegraph/entrypoint.sh"]
CMD ["serve"]
