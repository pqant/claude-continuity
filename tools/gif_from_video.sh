#!/usr/bin/env bash
# Screen recording -> README-ready GIF (two-pass palette, small file).
#
# Single-segment mode:
#   tools/gif_from_video.sh <input.mov|mp4> <output.gif> [maxwidth] [start] [duration]
#     maxwidth   default 800 (README-friendly)
#     start      optional trim start in seconds (e.g. 12.5)
#     duration   optional trim length in seconds (e.g. 90)
#
# Segments mode (multi-window cut list; ONE palette over the whole GIF):
#   tools/gif_from_video.sh --segments <segments.txt> <input> <output.gif> [maxwidth] [fps]
#     segments.txt: one window per line:  IN OUT SPEED   (seconds; SPEED = playback multiplier)
#                   '#' comments and blank lines are ignored
#                   example: tools/demo_segments.txt (the demo GIF cut list)
#     pipeline: per-segment x264 cut (uniform codec/res/fps) -> stream-copy concat
#               -> single palettegen/paletteuse pass over the concat
#     env knobs (segments mode):
#       GIF_MAX_COLORS   palettegen max_colors (default 256; 128/64 for smaller files)
#       GIF_DITHER       paletteuse dither (default bayer; use none for flat-color screens)
#     env knobs (both modes):
#       GIF_LOSSY        if set (e.g. 80), gifsicle -O3 also uses --lossy=<val>
#     If gifsicle is installed, a lossless `-O3` frame-delta pass runs at the end
#     (typically -20..40% size on terminal recordings).
#
# Tips:
#   - Cut the model-waiting gaps: run this once per interesting segment,
#     then concatenate the GIFs (e.g. with `ffmpeg -f concat`).
#   - 2x speed for long waits:
#       ffmpeg -ss 12 -t 60 -i in.mov -filter:v "setpts=0.5*PTS,fps=12,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer" -y out.gif
#   - Target: < ~2 MB for the README.
set -euo pipefail

SEGMENTS_MODE=0
if [[ "${1:-}" == "--segments" ]]; then
  SEGMENTS_MODE=1
  shift
fi

if [[ $SEGMENTS_MODE -eq 1 ]]; then
  if [[ $# -lt 3 ]]; then
    echo "usage: $0 --segments <segments.txt> <input> <output.gif> [maxwidth] [fps]" >&2
    exit 1
  fi
  SEGF="$1"; IN="$2"; OUT="$3"; W="${4:-700}"; FPS="${5:-12}"
  MAX_COLORS="${GIF_MAX_COLORS:-256}"
  DITHER="${GIF_DITHER:-bayer}"

  command -v ffmpeg >/dev/null || { echo "ffmpeg not found (brew install ffmpeg)" >&2; exit 1; }
  [[ -f "$SEGF" ]] || { echo "segments file not found: $SEGF" >&2; exit 1; }
  [[ -f "$IN" ]] || { echo "input video not found: $IN" >&2; exit 1; }

  TMPD="$(mktemp -d)"
  trap 'rm -rf "$TMPD"' EXIT

  # 1) per-segment cuts; uniform codec/resolution/fps so stream-copy concat is safe
  i=0
  : > "$TMPD/list.txt"
  while IFS=' ' read -r s_in s_out s_speed _rest; do
    [[ -z "${s_in:-}" || "$s_in" == \#* ]] && continue
    i=$((i + 1))
    seg="$TMPD/seg$(printf '%02d' "$i").mp4"
    dur="$(LC_ALL=C awk -v a="$s_in" -v b="$s_out" 'BEGIN{printf "%.3f", b - a}')"
    echo "  seg$(printf '%02d' "$i"): ${s_in}s -> ${s_out}s @${s_speed}x"
    # -nostdin: ffmpeg must not read from stdin (it would eat the segments file inside this loop)
    # -ss AND -t as INPUT options: read exactly the source window [IN, OUT].
    # (With -t as an OUTPUT option, setpts speedup makes ffmpeg read SPEED x more source
    #  than intended — verified broken on VFR screen recordings.)
    # input -ss is frame-accurate in modern ffmpeg: seeks keyframe, decodes to exact frame
    ffmpeg -nostdin -hide_banner -loglevel error -ss "$s_in" -t "$dur" -i "$IN" \
      -vf "setpts=PTS/${s_speed},fps=${FPS},scale=${W}:-2:flags=lanczos" \
      -c:v libx264 -crf 16 -preset veryfast -pix_fmt yuv420p -an "$seg"
    printf "file '%s'\n" "$seg" >> "$TMPD/list.txt"
  done < "$SEGF"
  [[ $i -gt 0 ]] || { echo "no segments parsed from $SEGF" >&2; exit 1; }

  # 2) concat (stream copy: all segments share codec/res/fps)
  ffmpeg -nostdin -hide_banner -loglevel error -f concat -safe 0 -i "$TMPD/list.txt" -c copy "$TMPD/concat.mp4"

  # 3) ONE palette over the whole GIF (consistent colors across scenes)
  ffmpeg -nostdin -hide_banner -loglevel error -i "$TMPD/concat.mp4" \
    -vf "split[a][b];[a]palettegen=stats_mode=diff:max_colors=${MAX_COLORS}[p];[b][p]paletteuse=dither=${DITHER}" \
    -y "$OUT"
else
  if [[ $# -lt 2 ]]; then
    echo "usage: $0 <input> <output.gif> [maxwidth] [start] [duration]" >&2
    exit 1
  fi
  IN="$1"; OUT="$2"; W="${3:-800}"; SS="${4:-}"; T="${5:-}"

  command -v ffmpeg >/dev/null || { echo "ffmpeg not found (brew install ffmpeg)" >&2; exit 1; }

  VF="fps=12,scale=$W:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer"

  if [[ -n "$SS" && -n "$T" ]]; then
    ffmpeg -nostdin -hide_banner -loglevel error -ss "$SS" -i "$IN" -t "$T" -vf "$VF" -y "$OUT"
  elif [[ -n "$SS" ]]; then
    ffmpeg -nostdin -hide_banner -loglevel error -ss "$SS" -i "$IN" -vf "$VF" -y "$OUT"
  else
    ffmpeg -nostdin -hide_banner -loglevel error -i "$IN" -vf "$VF" -y "$OUT"
  fi
fi

# 4) optional lossless frame-delta optimization (runs if gifsicle is installed)
if command -v gifsicle >/dev/null; then
  tmpgif="$(mktemp)"
  if [[ -n "${GIF_LOSSY:-}" ]]; then
    gifsicle -O3 --lossy="$GIF_LOSSY" "$OUT" -o "$tmpgif"
  else
    gifsicle -O3 "$OUT" -o "$tmpgif"
  fi
  mv "$tmpgif" "$OUT"
  echo "  gifsicle -O3 applied${GIF_LOSSY:+ (--lossy=$GIF_LOSSY)}"
fi

ls -lh "$OUT"
