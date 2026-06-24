# live-sim

Generates a DASH live stream with FFmpeg and pushes segments + the MPD to the next service in the pipeline (stream-lens, or morpheus directly if stream-lens is bypassed).

## Pipeline position

```
live-sim ──(MPD + segments)──▶ stream-lens ──(MPD)──▶ morpheus ──▶ player
```

Segments from **all** renditions are pushed. The stream-lens selects which rendition to buffer for analysis via `ANALYSIS_VIDEO_RENDITION`.

## How it works

1. Starts an FFmpeg process that generates a DASH stream into `media/segments/`.
2. A background loop scans the segments directory and HTTP PUTs new `.m4s` files to `OUTPUT_URL/segment`.
3. A separate patcher loop re-reads `live.mpd` every `PATCH_INTERVAL` seconds, injects active SCTE-35 events, and PUTs the updated manifest to `OUTPUT_URL/live.mpd`.

### Multi-rendition

`LIVE_SIM_RENDITIONS` controls the video ladder. Each entry produces one DASH adaptation set. Stream indices are assigned in order:

```
LIVE_SIM_RENDITIONS=1920x1080:4000k,1280x720:2000k,854x480:1000k
#                   → stream0          → stream1       → stream2
# audio                                                → stream3
```

Init segments (`init-stream{N}.m4s`) and media segments (`chunk-stream{N}-XXXXX.m4s`) carry the stream index in their filename — no extra headers needed to identify a rendition.

### Source modes

| Mode | When | Notes |
|------|------|-------|
| **File** | `LIVE_SIM_VIDEO` is set and file exists | Loops the file; uses file's audio; no timecode overlay |
| **Synthetic** | `LIVE_SIM_VIDEO` unset or file missing | SMPTE bars + beep; LOCAL/UTC timecode overlay on stream0 |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PATCH_INTERVAL` | `2` | MPD patch + push interval (seconds) |
| `LIVE_SIM_VIDEO` | — | Path to source video inside container (optional) |
| `LIVE_SIM_RENDITIONS` | `1920x1080:4000k` | Video rendition ladder, CSV `WxH:BITRATEk` |
| `OUTPUT_URL` | `http://morpheus` | Destination for segments and MPD pushes |
| `VAST2SGAI_URL` | `http://vast-2-sgai:3000` | vast-2-sgai service URL |
| `REAL_TIME_AD_GEN_URL` | `http://api:8000` | Real-time ad-gen API URL |
| `SERVER_PORT` | `8000` | HTTP port the control API listens on |

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/start` | Start the FFmpeg process |
| `POST` | `/api/stop` | Stop the FFmpeg process |
| `POST` | `/api/config` | Update `LIVE_SIM_VIDEO` at runtime (no restart needed) |
| `GET` | `/api/status` | FFmpeg state, active SCTE-35 events, recent log lines |
| `POST` | `/api/event` | Inject a SCTE-35 ad break or overlay event |
| `DELETE` | `/api/event/{id}` | Remove a pending event |

## Running

```bash
# From repo root via Docker Compose
docker compose up live-sim

# Standalone
docker run -p 8000:8000 \
  -e OUTPUT_URL=http://stream-lens:8001 \
  -e LIVE_SIM_RENDITIONS="1920x1080:4000k,1280x720:2000k,854x480:1000k" \
  -v ./live-sim/media:/media \
  live-sim
```

Set `LIVE_SIM_VIDEO=/media/yourfile.mp4` to use a local video instead of the synthetic source.
