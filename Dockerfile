FROM debian:bookworm-slim AS builder

ARG MODEL_URL_BASE
ENV MODEL_URL_BASE=${MODEL_URL_BASE}

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    autoconf \
    automake \
    libtool \
    pkg-config \
    wget \
  python3 \
  python3-pip \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . .

RUN ./download_model.sh
RUN set -e; \
  if [ -f rnnoise_data.c ]; then mv rnnoise_data.c src/; fi; \
  if [ -f rnnoise_data.h ]; then mv rnnoise_data.h src/; fi; \
  for f in $(find . -maxdepth 2 \( -name rnnoise_data.c -o -name rnnoise_data.h \)); do \
    case "$f" in */src/*) ;; *) mv "$f" src/ ;; esac; \
  done

RUN rm -rf build && cmake -S . -B build \
  -DRNNOISE_ENABLE_X86_RTCD=ON \
  -DRNNOISE_BUILD_SHARED=ON \
  -DRNNOISE_BUILD_STATIC=OFF \
  -DRNNOISE_BUILD_EXAMPLES=ON \
  -DRNNOISE_BUILD_TOOLS=OFF
RUN cmake --build build -j
RUN cmake --install build --prefix /opt/rnnoise
RUN mkdir -p /opt/rnnoise/bin /opt/rnnoise/models && \
  cp build/rnnoise_demo build/rnnoise_wrapper_demo /opt/rnnoise/bin/ && \
  if [ -f build/dump_weights_blob ]; then \
    (cd build && ./dump_weights_blob && cp weights_blob.bin /opt/rnnoise/models/); \
  elif [ -f src/rnnoise_data.c ]; then \
    echo "Model embedded in binary"; \
  else \
    echo "Warning: No model available" >&2; \
  fi

RUN pip3 install --no-cache-dir --break-system-packages -r server/requirements.txt

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ffmpeg \
  && rm -rf /var/lib/apt/lists/*

ENV RNNOISE_HOME=/opt/rnnoise
ENV PATH=/opt/rnnoise/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/rnnoise/lib

WORKDIR /app
COPY --from=builder /opt/rnnoise /opt/rnnoise
COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY server /app/server
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
