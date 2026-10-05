#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_DIR="/home/florian/bsc-app/radio"
readonly UV_BIN="/home/florian/.local/bin/uv"
readonly FLOCK_BIN="/usr/bin/flock"
readonly SCHEDULER_LOCK="$ROOT_DIR/data/scheduler.lock"
readonly TIMEZONE="Europe/Paris"

declare -Ar DEFAULT_HOURS=(
  [journal]=48
  [headlines]=24
  [awards]=744
  [week]=168
  [portrait]=720
  [recipe]=720
  [style]=720
  [glossary]=720
  [feedback]=720
)

declare -Ar DEFAULT_TITLES=(
  [journal]="Le journal du"
  [headlines]="Les titres du"
  [awards]="Le palmarès du"
  [week]="La semaine du"
  [portrait]="La visite du"
  [recipe]="La recette du"
  [style]="Le style du"
  [glossary]="Le mot du brasseur du"
  [feedback]="Le courrier des auditeurs du"
)

usage() {
  cat <<'EOF'
Usage: deploy/run-format.sh FORMAT [HOURS] [TITLE_PREFIX]

Formats:
  journal headlines awards week portrait recipe style glossary feedback

Exemples:
  deploy/run-format.sh week
  deploy/run-format.sh journal 72
  deploy/run-format.sh portrait 720 "Portrait brassicole du"

La collecte doit être planifiée séparément. Ce lancement utilise --skip-collect.
EOF
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

format=${1:-}
if [[ -z ${DEFAULT_HOURS[$format]+configured} ]]; then
  printf 'Format inconnu: %s\n\n' "${format:-<vide>}" >&2
  usage >&2
  exit 2
fi

hours=${2:-${DEFAULT_HOURS[$format]}}
title_prefix=${3:-${DEFAULT_TITLES[$format]}}

if [[ ! $hours =~ ^[1-9][0-9]*$ ]]; then
  printf 'La durée doit être un nombre entier positif: %s\n' "$hours" >&2
  exit 2
fi

if [[ ! -x $UV_BIN ]]; then
  printf 'uv est introuvable ou non exécutable: %s\n' "$UV_BIN" >&2
  exit 1
fi

if [[ ! -x $FLOCK_BIN ]]; then
  printf 'flock est introuvable ou non exécutable: %s\n' "$FLOCK_BIN" >&2
  exit 1
fi

day_names=("" "lundi" "mardi" "mercredi" "jeudi" "vendredi" "samedi" "dimanche")
month_names=("" "janvier" "février" "mars" "avril" "mai" "juin" "juillet" "août" "septembre" "octobre" "novembre" "décembre")

weekday=$(TZ=$TIMEZONE date +%u)
day=$(TZ=$TIMEZONE date +%-d)
month_number=$(TZ=$TIMEZONE date +%-m)
year=$(TZ=$TIMEZONE date +%Y)
date_fr="${day_names[$weekday]} ${day} ${month_names[$month_number]} ${year}"
unit="houblon-radio-${format}"
title="${title_prefix} ${date_fr}"

printf 'Lancement de %s : %s (%s h)\n' "$unit" "$title" "$hours"

if [[ ${RADIO_RUN_FOREGROUND:-0} == 1 ]]; then
  exec "$FLOCK_BIN" --wait 21600 "$SCHEDULER_LOCK" \
    "$UV_BIN" run radio run \
    --fmt "$format" \
    --title "$title" \
    --hours "$hours" \
    --skip-collect
fi

exec systemd-run --user \
  --unit="$unit" \
  --collect \
  --property="WorkingDirectory=$ROOT_DIR" \
  --property="Environment=PATH=/home/florian/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  --property=TimeoutStartSec=infinity \
  "$FLOCK_BIN" --wait 21600 "$SCHEDULER_LOCK" \
  "$UV_BIN" run radio run \
  --fmt "$format" \
  --title "$title" \
  --hours "$hours" \
  --skip-collect
