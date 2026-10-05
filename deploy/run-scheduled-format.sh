#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_DIR="/home/florian/bsc-app/radio"
format=${1:-}

if [[ -z $format ]]; then
  echo "Un format planifié est requis" >&2
  exit 2
fi

if [[ $format == journal ]]; then
  exec "$ROOT_DIR/deploy/run-format.sh" journal 96
fi

exec "$ROOT_DIR/deploy/run-format.sh" "$format"
