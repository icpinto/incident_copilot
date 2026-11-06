FROM timberio/vector:0.61.0-debian

USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY inventory.py /etc/vector/inventory.py

COPY start.py /etc/vector/start.py
ENTRYPOINT ["python3", "/etc/vector/start.py"]
CMD ["--config", "/etc/vector/vector.yaml"]
