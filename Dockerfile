FROM python:3-slim

RUN python3 -m pip install --upgrade container-cron && \
    echo '#!/bin/sh\n/usr/local/bin/container-cron --cri-socket=$CONTAINER_SOCKET --namespace=$CONTAINER_NAMESPACE' > /start && \
    chmod +x /start

ENV CONTAINER_NAMESPACE=k8s.io
ENV CONTAINER_SOCKET=/run/containerd/containerd.sock

CMD ["/start"]