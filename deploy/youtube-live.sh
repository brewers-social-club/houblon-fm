#!/bin/bash
set -euo pipefail

if [[ -z "${YOUTUBE_STREAM_KEY:-}" ]]; then
  echo "YOUTUBE_STREAM_KEY is required" >&2
  exit 64
fi

rtmps_url="${YOUTUBE_RTMPS_URL:-rtmps://a.rtmps.youtube.com:443/live2}"
audio_url="${YOUTUBE_AUDIO_URL:-http://icecast:8000/live.mp3}"
artwork="${YOUTUBE_ARTWORK:-/artwork/houblon-fm-logo.png}"
redactor="${YOUTUBE_REDACTOR:-/usr/local/bin/redact-ffmpeg}"

if [[ ! -r "$artwork" ]]; then
  echo "YouTube artwork is missing or unreadable: $artwork" >&2
  exit 66
fi

if [[ ! -x "$redactor" ]]; then
  echo "FFmpeg log redactor is missing or not executable: $redactor" >&2
  exit 66
fi

echo "Waiting for the Houblon FM Icecast stream..."
until (set +o pipefail; curl --fail --silent --max-time 5 "$audio_url" | head -c 1 >/dev/null); do
  sleep 5
done

echo "Starting the encrypted YouTube Live relay (720p30, AAC 128 kb/s)."

target="${rtmps_url%/}/${YOUTUBE_STREAM_KEY}"
exec ffmpeg \
  -hide_banner \
  -nostats \
  -loglevel warning \
  -loop 1 \
  -framerate 2 \
  -thread_queue_size 512 \
  -i "$artwork" \
  -reconnect 1 \
  -reconnect_on_network_error 1 \
  -reconnect_streamed 1 \
  -reconnect_delay_max 5 \
  -thread_queue_size 512 \
  -i "$audio_url" \
  -map 0:v:0 \
  -map 1:a:0 \
  -vf "scale=620:620:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x0b0806,fps=30,format=yuv420p" \
  -c:v libx264 \
  -preset veryfast \
  -tune stillimage \
  -profile:v high \
  -level 3.1 \
  -b:v 4M \
  -minrate 4M \
  -maxrate 4M \
  -bufsize 8M \
  -g 60 \
  -keyint_min 60 \
  -sc_threshold 0 \
  -c:a aac \
  -b:a 128k \
  -ar 44100 \
  -ac 2 \
  -f flv \
  "$target" \
  2> >("$redactor")
