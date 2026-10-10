#!/bin/sh
# api-entrypoint.sh: container entrypoint for the video-factory API image.
#
# The data volume may predate the current image (named volumes are only
# seeded from the image on first creation), so make sure the writable
# directories exist before the app starts. TMPDIR lives on the volume:
# ffmpeg temp files can be large and a tmpfs would count against the
# container memory limit. Python's tempfile silently falls back to /tmp
# when TMPDIR is missing, so creating it here matters.
set -eu

for dir in "${VIDEO_FACTORY_STORAGE:-/data/storage}" "${TMPDIR:-/data/tmp}"; do
  mkdir -p "$dir"
done
if [ -n "${VIDEO_FACTORY_DB:-}" ]; then
  mkdir -p "$(dirname "$VIDEO_FACTORY_DB")"
fi

exec "$@"
