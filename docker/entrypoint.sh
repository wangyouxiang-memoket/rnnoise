#!/bin/sh
set -e

mode=${RNNOISE_MODE:-wrapper}

case "$mode" in
  server)
    exec uvicorn server.app:APP --host 0.0.0.0 --port 8000
    ;;
  demo|wrapper)
    if [ "$#" -lt 2 ]; then
      echo "Usage: $0 <noisy.pcm> <denoised.pcm> [model.bin]"
      echo "Set RNNOISE_MODE=demo to use rnnoise_demo (default: wrapper)."
      exit 1
    fi
    if [ "$mode" = "demo" ]; then
      exec rnnoise_demo "$@"
    else
      exec rnnoise_wrapper_demo "$@"
    fi
    ;;
  *)
    echo "Unknown RNNOISE_MODE: $mode"
    exit 1
    ;;
esac
