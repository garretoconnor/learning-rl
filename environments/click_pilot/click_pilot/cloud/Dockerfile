FROM __PYTHON_BASE__ AS execution
COPY requirements-runtime.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && pip check

FROM __PYTHON_BASE__
RUN apt-get update && apt-get install -y --no-install-recommends docker.io ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=execution / /opt/click-pilot/rootfs/
RUN tar -C /opt/click-pilot/rootfs -cf /opt/click-pilot/exec-rootfs.tar . && rm -rf /opt/click-pilot/rootfs
COPY requirements-controller.txt /tmp/requirements-controller.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements-controller.txt && pip check
COPY supervisor.py worker.py /opt/click-pilot/
WORKDIR /app
