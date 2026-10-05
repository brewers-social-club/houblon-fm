#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_DIR="/home/florian/bsc-app/radio"
readonly UNIT_DIR="$ROOT_DIR/deploy/systemd/user"

mapfile -t unit_files < <(find "$UNIT_DIR" -maxdepth 1 -type f \( -name '*.service' -o -name '*.timer' \) -print | sort)
mapfile -t timer_names < <(find "$UNIT_DIR" -maxdepth 1 -type f -name '*.timer' -printf '%f\n' | sort)

if (( ${#unit_files[@]} == 0 || ${#timer_names[@]} == 0 )); then
  echo "Aucune unité systemd Houblon FM trouvée dans $UNIT_DIR" >&2
  exit 1
fi

systemctl --user link "${unit_files[@]}"
systemctl --user daemon-reload
systemctl --user enable --now "${timer_names[@]}"
systemctl --user list-timers --all --no-pager 'houblon-radio-*'
