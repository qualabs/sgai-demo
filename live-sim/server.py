#!/usr/bin/env python3
"""
Live-Sim: DASH live streaming simulator with SCTE-35 event injection.

Flow:
  FFmpeg (loops bbb_320x180.mp4) → segments + live.mpd (dynamic)
  Patcher loop (every 2s): read live.mpd + inject SCTE-35 → PUT to stream-lens → Morpheus
  Segment pusher: stream new .m4s files → stream-lens → Morpheus (stored in /dev/shm)
  Morpheus (port 80): SCTE-35 → <ReplacePresentation> → serves patched MPD + segments
  GET /live.mpd: proxy from Morpheus (debug/dashboard use)
  GET /segments/*: serve FFmpeg segments (debug/dashboard use)
  GET /api/list-mpd: SGAI endpoint → alternative content MPD
"""

import asyncio
import collections
import io
import json
import logging
import os
import re
import signal
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

from aiohttp import web, ClientSession, ClientTimeout

# ── Bootstrap ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

config.load()

# ── Constants ────────────────────────────────────────────────────────────────

SEGMENTS_DIR = Path(__file__).parent / "media" / "segments"
STATIC_DIR   = Path(__file__).parent / "static"
MPD_PATH         = SEGMENTS_DIR / "live.mpd"
PATCHED_MPD_PATH = SEGMENTS_DIR / "live_scte35.mpd"
OVERLAYS_DIR     = Path(__file__).parent / "overlays"

VAST2SGAI_URL         = config.get("VAST2SGAI_URL", "http://localhost:3000")
REAL_TIME_AD_GEN_URL  = config.get("REAL_TIME_AD_GEN_URL", "http://ad-gen-api:8000")
SERVER_PORT      = config.get_int("SERVER_PORT", 8000)
PATCH_INTERVAL   = config.get_int("PATCH_INTERVAL", 2)
MORPHEUS_URL     = config.get("MORPHEUS_URL", "http://morpheus")


# Shared output/encoding tail — identical for both the synthetic and the
# file-backed source. Defined once so the two branches can't drift.
OUTPUT_TAIL = [
    "-c:a", "aac", "-b:a", "128k",
    "-f", "dash",
    "-streaming", "1",
    "-seg_duration", "2",
    "-window_size", "10",
    "-extra_window_size", "5",
    "-use_timeline", "1",
    "-use_template", "1",
    "-remove_at_exit", "0",
    str(MPD_PATH),
]


def _parse_renditions(env_str: str) -> list[dict]:
    result = []
    for entry in env_str.split(","):
        res, bitrate = entry.strip().split(":")
        w, h = res.split("x")
        result.append({"width": int(w), "height": int(h), "bitrate": bitrate})
    return result

_renditions: list[dict] = _parse_renditions(os.environ.get("LIVE_SIM_RENDITIONS", "1920x1080:4000k"))
_num_video_renditions: int = len(_renditions)


def _res_drawtext(r: dict) -> str:
    """Resolution label burned into the top-right corner of a rendition."""
    label = f"{r['width']}x{r['height']}"
    return (
        "drawtext="
        "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Mono.ttf:"
        f"text='{label}':"
        "fontcolor=white:fontsize=36:box=1:boxcolor=black@0.8:"
        "x=w-text_w-10:y=10"
    )


def _build_filter_complex(renditions: list[dict], video_input: str = "0:v", synthetic: bool = False) -> str:
    """Build a filter_complex string that splits one video input into N scaled renditions.

    Each rendition gets its own scale + resolution label. Stream 0 in synthetic
    mode also gets the realtime throttle + LOCAL/UTC timecode overlay.
    """
    n = len(renditions)

    _timecode_drawtext = (
        "drawtext="
        "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Mono.ttf:"
        r"text='LOCAL\: %{localtime\:%T}.%{eif\:mod(t\,1)*1000\:d\:3}':"
        "fontcolor=white:fontsize=80:box=1:boxcolor=black@0.8:"
        "x=(w-text_w)/2:y=(h-text_h)/2-60,"
        "drawtext="
        "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Mono.ttf:"
        r"text='UTC\: %{gmtime\:%T}.%{eif\:mod(t\,1)*1000\:d\:3}':"
        "fontcolor=yellow:fontsize=80:box=1:boxcolor=black@0.8:"
        "x=(w-text_w)/2:y=(h-text_h)/2+60"
    )

    split_outs = "".join(f"[v{i}]" for i in range(n))
    chains = [f"[{video_input}]split={n}{split_outs}"]

    for i, r in enumerate(renditions):
        filters = [f"scale={r['width']}:{r['height']}"]
        if synthetic and i == 0:
            filters += ["realtime", _timecode_drawtext]
        filters.append(_res_drawtext(r))
        chains.append(f"[v{i}]{''.join(f'{f}' if j == 0 else f',{f}' for j, f in enumerate(filters))}[out{i}]")

    return ";".join(chains)


def build_ffmpeg_cmd() -> list[str]:
    """Build the FFmpeg command.

    Uses filter_complex with an explicit split so each rendition gets its own
    independent scale + overlay chain. This is required in FFmpeg 7 where
    per-stream -vf specifiers do not reliably isolate scale filters.

    - File mode: loops the file, uses its audio track.
    - Synthetic mode: SMPTE bars + beep; stream0 gets timecode overlay.
    Both modes burn the resolution label into every rendition.
    """
    n = len(_renditions)
    video = _live_sim_video

    per_stream: list[str] = []
    for i, r in enumerate(_renditions):
        per_stream += [
            f"-c:v:{i}", "libx264",
            f"-preset:v:{i}", "ultrafast",
            f"-tune:v:{i}", "zerolatency",
            f"-b:v:{i}", r["bitrate"],
            f"-g:v:{i}", "60",
            f"-pix_fmt:v:{i}", "yuv420p",
        ]

    video_maps = []
    for i in range(n):
        video_maps += ["-map", f"[out{i}]"]

    # Group all video streams into one AdaptationSet so dash.js sees them as
    # quality levels it can switch between, not separate tracks.
    video_streams = ",".join(str(i) for i in range(n))
    adaptation_sets = f"id=0,streams={video_streams} id=1,streams={n}"
    output_tail = OUTPUT_TAIL[:-1] + ["-adaptation_sets", adaptation_sets, OUTPUT_TAIL[-1]]

    if video and os.path.isfile(video):
        source = ["ffmpeg", "-stream_loop", "-1", "-re", "-i", video]
        fc = _build_filter_complex(_renditions, video_input="0:v", synthetic=False)
        return source + ["-filter_complex", fc] + video_maps + ["-map", "0:a?"] + per_stream + output_tail

    # Synthetic path — SMPTE bars + beep audio
    source = [
        "ffmpeg",
        "-re",
        "-f", "lavfi", "-i", "smptehdbars=size=1920x1080:rate=30",
        "-f", "lavfi", "-i", "aevalsrc=exprs='if(lt(mod(t\\,1)\\,0.08)\\,sin(2*PI*1000*t)\\,0)|if(lt(mod(t\\,1)\\,0.08)\\,sin(2*PI*1000*t)\\,0):sample_rate=48000",
    ]
    fc = _build_filter_complex(_renditions, video_input="0:v", synthetic=True)
    return source + ["-filter_complex", fc] + video_maps + ["-map", "1:a"] + per_stream + output_tail

# ── State ─────────────────────────────────────────────────────────────────────

# Runtime-configurable source video. None → synthetic SMPTE-bars source.
# Updated via POST /api/config without restarting the container.
_live_sim_video: str | None = os.environ.get("LIVE_SIM_VIDEO") or None

ffmpeg_proc:    asyncio.subprocess.Process | None = None
ffmpeg_start:   datetime | None = None   # UTC time FFmpeg was launched
pending_events: list[dict]               = []    # active SCTE-35 events
event_counter:  int                      = 0     # local event id counter
ffmpeg_log:     collections.deque        = collections.deque(maxlen=200)

OUTPUT_URL       = config.get("OUTPUT_URL", "http://morpheus")
MPD_PUSH_URL     = f"{OUTPUT_URL}/live.mpd"
SEGMENT_PUSH_URL = f"{OUTPUT_URL}/segment"

_pushed_init:     dict[int, bool] = {}
_pushed_segments: set[str]        = set()

# ── Namespace helpers ─────────────────────────────────────────────────────────

DASH_NS   = "urn:mpeg:dash:schema:mpd:2011"
SCTE35_NS = "http://www.scte.org/schemas/35/2016"

ET.register_namespace("", DASH_NS)
ET.register_namespace("scte35", SCTE35_NS)
ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")

# ── MPD patching ──────────────────────────────────────────────────────────────

def _ns(tag: str) -> str:
    return f"{{{DASH_NS}}}{tag}"


def _scte_ns(tag: str) -> str:
    return f"{{{SCTE35_NS}}}{tag}"


def _reformat_mpd_tag(xml_str: str) -> str:
    """Break <MPD attr="val" ...> opening tag across multiple lines for readability."""
    def replacer(m):
        attrs = re.findall(r'[\w:.-]+=(?:"[^"]*"|\'[^\']*\')', m.group(1))
        return '<MPD\n\t' + '\n\t'.join(attrs) + '>'
    return re.sub(r'<MPD\s+([^>]+)>', replacer, xml_str, count=1)


def build_scte35_event_stream(events: list[dict]) -> ET.Element:
    """Build a SCTE-35 EventStream element for insertion into the MPD Period."""
    es = ET.Element("EventStream")
    es.set("schemeIdUri", "urn:scte:scte35:2013:xml")
    es.set("timescale", "1000")

    for ev in events:
        event_el = ET.SubElement(es, "Event")
        event_el.set("presentationTime", str(ev["presentation_time_ms"]))
        event_el.set("duration", str(ev["duration_ms"]))
        event_el.set("id", str(ev["id"]))

        section = ET.SubElement(event_el, _scte_ns("SpliceInfoSection"))
        section.set("xmlns:scte35", SCTE35_NS)

        if ev.get("type") == "overlay":
            desc = ET.SubElement(section, _scte_ns("SegmentationDescriptor"))
            desc.set("segmentationEventId", f"0x{ev['id']:08x}")
            desc.set("segmentationTypeId", "56")
            upid = ET.SubElement(desc, _scte_ns("SegmentationUpid"))
            upid.set("segmentationUpidType", "14")
            upid.set("segmentationUpidFormat", "text")
            upid.text = f"shape={ev['shape']}"
        else:
            insert = ET.SubElement(section, _scte_ns("SpliceInsert"))
            insert.set("spliceEventId", "1")
            insert.set("outOfNetworkIndicator", "true")
            break_dur = ET.SubElement(insert, _scte_ns("BreakDuration"))
            break_dur.set("autoReturn", "true")
            break_dur.set("duration", str(ev["duration_ms"]))

    return es


def patch_mpd(raw_xml: str, active_events: list[dict]) -> str:
    """
    Patch the FFmpeg-generated MPD:
      - add BaseURL pointing to our segments endpoint
      - inject SCTE-35 EventStream if there are active events
    Returns the patched MPD as a UTF-8 string.
    """
    # Parse preserving namespaces
    root = ET.fromstring(raw_xml)

    # Sync player MPD refresh rate with our patch interval
    root.set("minimumUpdatePeriod", f"PT{PATCH_INTERVAL}S")
    # Give the player 3 segments of buffer behind the live edge (absorbs ffmpeg -re jitter)
    root.set("suggestedPresentationDelay", "PT3S")

    # Find Period (handle with or without namespace)
    period = root.find(_ns("Period"))
    if period is None:
        period = root.find("Period")
    if period is None:
        raise ValueError("No <Period> found in MPD")

    # No BaseURL — segments are served directly from morpheus. The player
    # resolves segment URLs relative to the manifest URL (morpheus root).

    # Inject SCTE-35 EventStream before AdaptationSets
    if active_events:
        scte_es = build_scte35_event_stream(active_events)
        period.insert(0, scte_es)

    ET.indent(root, space="\t")
    xml_str = ET.tostring(root, encoding="unicode", xml_declaration=True)
    return _reformat_mpd_tag(xml_str)


async def patcher_loop():
    """Background task: every PATCH_INTERVAL seconds, read live.mpd, patch it, PUT to Morpheus."""
    global pending_events

    await asyncio.sleep(4)  # Give FFmpeg time to write the first MPD

    async with ClientSession(timeout=ClientTimeout(total=5)) as session:
        while True:
            try:
                if ffmpeg_proc is not None and ffmpeg_proc.returncode is None:
                    if MPD_PATH.exists():
                        raw = MPD_PATH.read_text(encoding="utf-8")

                        now = datetime.now(timezone.utc)

                        # Expire old events
                        pending_events = [
                            e for e in pending_events
                            if now < e["expiry"]
                        ]

                        # Active = not yet expired
                        active = [e for e in pending_events if now < e["expiry"]]

                        patched = patch_mpd(raw, active)

                        PATCHED_MPD_PATH.write_text(patched, encoding="utf-8")

                        async with session.put(
                            MPD_PUSH_URL,
                            data=patched.encode("utf-8"),
                            headers={"Content-Type": "application/dash+xml"},
                        ) as resp:
                            if resp.status not in (200, 201, 204):
                                body = await resp.text()
                                logger.warning("[patcher] Morpheus PUT %s: %s", resp.status, body[:200])
                            else:
                                event_info = f" | {len(active)} event(s)" if active else ""
                                logger.info("[patcher] PUT OK%s", event_info)
            except Exception as exc:
                logger.error("[patcher] error: %s", exc)

            await asyncio.sleep(PATCH_INTERVAL)


async def _push_segment(
    session: ClientSession,
    url: str,
    data: bytes,
    seg_type: str,
    stream_type: str,
    seg_num: int | None,
    seg_name: str | None = None,
) -> int:
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Segment-Type": seg_type,
        "X-Stream-Type": stream_type,
    }
    if seg_num is not None:
        headers["X-Segment-Number"] = str(seg_num)
    if seg_name is not None:
        headers["X-Segment-Name"] = seg_name
    try:
        async with session.put(url, data=data, headers=headers) as resp:
            if resp.status not in (200, 201, 204):
                logger.debug("[pusher] PUT %s %s → %s", seg_type, stream_type, resp.status)
            return resp.status
    except Exception as exc:
        logger.debug("[pusher] PUT failed: %s", exc)
        return 0


async def segment_pusher_loop():
    """Background task: forward new DASH segments to stream-lens."""
    global _pushed_init, _pushed_segments

    segment_url = SEGMENT_PUSH_URL

    async with ClientSession(timeout=ClientTimeout(total=10)) as session:
        while True:
            try:
                if ffmpeg_proc is not None and ffmpeg_proc.returncode is None:
                    # Push init segments once per stream
                    for stream_id in range(_num_video_renditions + 1):
                        if not _pushed_init.get(stream_id, False):
                            stream_type = "video" if stream_id < _num_video_renditions else "audio"
                            p = SEGMENTS_DIR / f"init-stream{stream_id}.m4s"
                            if p.exists():
                                await _push_segment(
                                    session, segment_url, p.read_bytes(),
                                    "init", stream_type, None, f"init-stream{stream_id}.m4s"
                                )
                                _pushed_init[stream_id] = True

                    # Push new media segments
                    for seg_file in sorted(SEGMENTS_DIR.glob("chunk-stream*.m4s")):
                        if seg_file.name not in _pushed_segments:
                            m = re.match(r"chunk-stream(\d+)-(\d+)\.m4s", seg_file.name)
                            if m:
                                stream_type = "video" if int(m.group(1)) < _num_video_renditions else "audio"
                                seg_num = int(m.group(2))
                                status = await _push_segment(session, segment_url, seg_file.read_bytes(), "media", stream_type, seg_num, seg_file.name)
                                if status == 409:
                                    # stream-lens restarted and lost init state — re-push next iteration
                                    logger.info("[pusher] stream-lens lost state (409), resetting push flags")
                                    _pushed_init = {}
                                    _pushed_segments.clear()
                                    break
                                _pushed_segments.add(seg_file.name)
                else:
                    # Reset when stream stops
                    _pushed_init = {}
                    _pushed_segments.clear()

            except Exception as exc:
                logger.debug("[pusher] error: %s", exc)

            await asyncio.sleep(PATCH_INTERVAL)


# ── HTTP handlers ─────────────────────────────────────────────────────────────

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


async def handle_index(request: web.Request) -> web.Response:
    index = STATIC_DIR / "index.html"
    return web.Response(
        text=index.read_text(),
        content_type="text/html",
        headers=CORS_HEADERS,
    )



async def handle_scte35_mpd(_request: web.Request) -> web.Response:
    """Serve the last SCTE-35-patched MPD written to disk (pre-Morpheus)."""
    if not PATCHED_MPD_PATH.exists():
        return web.Response(status=404, text="No patched MPD yet — start FFmpeg first", headers=CORS_HEADERS)
    return web.Response(
        text=PATCHED_MPD_PATH.read_text(encoding="utf-8"),
        content_type="application/dash+xml",
        headers=CORS_HEADERS,
    )


async def handle_segment(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    filepath = SEGMENTS_DIR / filename
    if not filepath.exists():
        return web.Response(status=404, headers=CORS_HEADERS)
    loop = asyncio.get_event_loop()
    body = await loop.run_in_executor(None, filepath.read_bytes)
    return web.Response(
        body=body,
        content_type="video/mp4",
        headers={**CORS_HEADERS, "Cache-Control": "no-cache"},
    )


async def handle_start(request: web.Request) -> web.Response:
    global ffmpeg_proc, ffmpeg_start, pending_events, event_counter
    global _pushed_init, _pushed_segments

    if ffmpeg_proc is not None and ffmpeg_proc.returncode is None:
        return web.json_response({"ok": False, "msg": "FFmpeg already running"}, headers=CORS_HEADERS)

    SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)

    # Remove stale MPD and any leftover segments from a previous run
    for _f in SEGMENTS_DIR.glob("*.mpd"):
        _f.unlink(missing_ok=True)
    for _f in SEGMENTS_DIR.glob("*.m4s"):
        _f.unlink(missing_ok=True)

    used_file = bool(_live_sim_video and os.path.isfile(_live_sim_video))
    logger.info("[ffmpeg] source=%s", _live_sim_video if used_file else "synthetic smptehdbars")

    cmd = build_ffmpeg_cmd()
    ffmpeg_proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    ffmpeg_start   = datetime.now(timezone.utc)
    pending_events = []
    event_counter  = 0
    ffmpeg_log.clear()
    _pushed_init = {}
    _pushed_segments.clear()
    asyncio.ensure_future(_read_stderr(ffmpeg_proc))

    logger.info("[ffmpeg] started pid=%s", ffmpeg_proc.pid)
    return web.json_response({"ok": True, "pid": ffmpeg_proc.pid}, headers=CORS_HEADERS)


async def handle_stop(request: web.Request) -> web.Response:
    global ffmpeg_proc, ffmpeg_start

    if ffmpeg_proc is None or ffmpeg_proc.returncode is not None:
        return web.json_response({"ok": False, "msg": "FFmpeg not running"}, headers=CORS_HEADERS)

    ffmpeg_proc.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(ffmpeg_proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        ffmpeg_proc.kill()

    logger.info("[ffmpeg] stopped")
    ffmpeg_proc  = None
    ffmpeg_start = None
    return web.json_response({"ok": True}, headers=CORS_HEADERS)


async def _read_stderr(proc: asyncio.subprocess.Process) -> None:
    """Drain ffmpeg stderr into ffmpeg_log to prevent buffer deadlock."""
    try:
        async for raw in proc.stderr:
            ffmpeg_log.append(raw.decode(errors="replace").rstrip())
    except Exception:
        pass


async def handle_status(request: web.Request) -> web.Response:
    now = datetime.now(timezone.utc)
    running = ffmpeg_proc is not None and ffmpeg_proc.returncode is None

    events_info = []
    for e in pending_events:
        fire_in = (
            (ffmpeg_start + timedelta(milliseconds=e["presentation_time_ms"]) - now).total_seconds()
            if ffmpeg_start is not None else None
        )
        expires_in = (e["expiry"] - now).total_seconds()
        events_info.append({
            "id":           e["id"],
            "type":         e.get("type", "replace"),
            "shape":        e.get("shape"),
            "fire_in_s":    round(fire_in, 1) if fire_in is not None else None,
            "duration_s":   round(e["duration_ms"] / 1000, 1),
            "expires_in_s": round(expires_in, 1),
        })

    return web.json_response(
        {
            "ffmpeg_running": running,
            "ffmpeg_pid":     ffmpeg_proc.pid if running else None,
            "events":         events_info,
            "current_video":  _live_sim_video,
        },
        headers=CORS_HEADERS,
    )


async def handle_logs(_request: web.Request) -> web.Response:
    return web.json_response({"lines": list(ffmpeg_log)}, headers=CORS_HEADERS)


async def handle_inject(request: web.Request) -> web.Response:
    global event_counter

    if ffmpeg_start is None:
        return web.json_response(
            {"ok": False, "msg": "FFmpeg not running — start it first"},
            headers=CORS_HEADERS,
        )

    body = await request.json()
    delay_s    = float(body.get("delay_s", 10))
    duration_s = float(body.get("duration_s", 30))

    now = datetime.now(timezone.utc)
    elapsed_ms = (now - ffmpeg_start).total_seconds() * 1000
    presentation_time_ms = int(elapsed_ms + delay_s * 1000)
    duration_ms          = int(duration_s * 1000)
    expiry               = ffmpeg_start + timedelta(milliseconds=presentation_time_ms + duration_ms)

    event_counter += 1
    event = {
        "id":                   event_counter,
        "presentation_time_ms": presentation_time_ms,
        "duration_ms":          duration_ms,
        "expiry":               expiry,
    }
    pending_events.append(event)

    logger.info("[inject] event id=%s pt=%sms dur=%sms", event_counter, presentation_time_ms, duration_ms)
    return web.json_response(
        {
            "ok":     True,
            "event":  {"id": event_counter, "delay_s": delay_s, "duration_s": duration_s},
        },
        headers=CORS_HEADERS,
    )


_VALID_SHAPES = {"banner", "skyscraper", "lshape-left", "lshape-right"}


async def handle_inject_overlay(request: web.Request) -> web.Response:
    global event_counter

    if ffmpeg_start is None:
        return web.json_response(
            {"ok": False, "msg": "FFmpeg not running — start it first"},
            headers=CORS_HEADERS,
        )

    body = await request.json()
    shape        = body.get("shape", "banner")
    delay_s      = float(body.get("delay_s", 10))
    duration_s   = float(body.get("duration_s", 20))
    if shape not in _VALID_SHAPES:
        return web.json_response(
            {"ok": False, "msg": f"Invalid shape. Must be one of: {', '.join(sorted(_VALID_SHAPES))}"},
            headers=CORS_HEADERS,
        )

    now = datetime.now(timezone.utc)
    elapsed_ms            = (now - ffmpeg_start).total_seconds() * 1000
    presentation_time_ms  = int(elapsed_ms + delay_s * 1000)
    duration_ms           = int(duration_s * 1000)
    expiry                = ffmpeg_start + timedelta(milliseconds=presentation_time_ms + duration_ms)

    event_counter += 1
    event = {
        "id":                   event_counter,
        "type":                 "overlay",
        "shape":                shape,
        "presentation_time_ms": presentation_time_ms,
        "duration_ms":          duration_ms,
        "expiry":               expiry,
    }
    pending_events.append(event)

    logger.info("[inject-overlay] id=%s shape=%s pt=%sms dur=%sms", event_counter, shape, presentation_time_ms, duration_ms)
    return web.json_response(
        {
            "ok":    True,
            "event": {"id": event_counter, "shape": shape, "delay_s": delay_s, "duration_s": duration_s},
        },
        headers=CORS_HEADERS,
    )


async def handle_list_mpd(request: web.Request) -> web.Response:
    """Proxy GET /api/list-mpd → vast-2-sgai, patch first-period presentationTime=0 events to 20ms.

    GET /api/list-mpd?vasturl=<url>
    """
    upstream_url = f"{VAST2SGAI_URL}/api/list-mpd"
    query_string = request.rel_url.query_string

    try:
        async with ClientSession(timeout=ClientTimeout(total=10)) as session:
            async with session.get(
                f"{upstream_url}?{query_string}" if query_string else upstream_url
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("[list-mpd] upstream %s: %s", resp.status, body[:200])
                    return web.Response(status=resp.status, text=body, headers=CORS_HEADERS)
                raw_xml = await resp.text()
    except Exception as exc:
        logger.error("[list-mpd] upstream error: %s", exc)
        return web.Response(status=502, text=f"Could not reach vast-2-sgai: {exc}", headers=CORS_HEADERS)

    # Patch: in first Period only, replace presentationTime="0" → ZERO_EVENT_DELAY_MS.
    # NOTE: ET.fromstring() cannot be used — the tracking URLs in Event text nodes contain
    # unescaped '&' characters, making the document not well-formed XML.
    # A targeted string replacement scoped to the first </Period> is simpler and safe.
    ZERO_EVENT_DELAY_MS = 20
    first_period_end = raw_xml.find("</Period>")
    if first_period_end != -1:
        patched = (
            raw_xml[:first_period_end].replace(
                'presentationTime="0"', f'presentationTime="{ZERO_EVENT_DELAY_MS}"'
            )
            + raw_xml[first_period_end:]
        )
    else:
        patched = raw_xml

    logger.info("[list-mpd] patched and returned ListMPD")
    return web.Response(
        text=patched,
        content_type="application/dash+xml",
        headers=CORS_HEADERS,
    )



async def handle_config(request: web.Request) -> web.Response:
    """Update live-sim runtime config without restarting the container.

    Accepted fields:
      LIVE_SIM_VIDEO: str | null  — path to the source video file, or null for
                                    the synthetic SMPTE-bars source.

    Does NOT stop or restart FFmpeg; the caller must do that around this call.
    Changes take effect on the next POST /api/start.
    """
    global _live_sim_video
    body = await request.json()
    if "LIVE_SIM_VIDEO" in body:
        val = body["LIVE_SIM_VIDEO"]
        _live_sim_video = str(val) if val else None
    return web.json_response(
        {"ok": True, "config": {"LIVE_SIM_VIDEO": _live_sim_video}},
        headers=CORS_HEADERS,
    )


async def handle_switch(request: web.Request) -> web.Response:
    """Switch input source with a full segment-store flush.

    Sequence:
      1. Stop FFmpeg (if running).
      2. Delete local .m4s / .mpd files from SEGMENTS_DIR.
      3. Delete pushed segments from Morpheus.
      4. Reset stream-lens buffer via POST /config.
      5. Update _live_sim_video.
      6. Reset push-tracking state.

    The caller must POST /api/start to begin streaming the new source.
    """
    global _live_sim_video, ffmpeg_proc, ffmpeg_start, _pushed_init, _pushed_segments

    body = await request.json()
    new_video = str(body["LIVE_SIM_VIDEO"]) if body.get("LIVE_SIM_VIDEO") else None

    # 1. Stop FFmpeg
    if ffmpeg_proc is not None and ffmpeg_proc.returncode is None:
        ffmpeg_proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(ffmpeg_proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            ffmpeg_proc.kill()
        ffmpeg_proc  = None
        ffmpeg_start = None
        logger.info("[switch] FFmpeg stopped")

    # 2. Clear local segment files
    for _f in SEGMENTS_DIR.glob("*.m4s"):
        _f.unlink(missing_ok=True)
    for _f in SEGMENTS_DIR.glob("*.mpd"):
        _f.unlink(missing_ok=True)
    logger.info("[switch] local segments cleared")

    # Snapshot pushed state before reset so we know what to delete remotely
    init_ids      = list(_pushed_init.keys())
    old_segments  = list(_pushed_segments)
    _pushed_init  = {}
    _pushed_segments.clear()
    _live_sim_video = new_video

    async with ClientSession(timeout=ClientTimeout(total=10)) as session:
        # 3. Delete old segments from Morpheus
        morpheus_files = (
            [f"init-stream{i}.m4s" for i in init_ids]
            + old_segments
            + ["live.mpd"]
        )
        for name in morpheus_files:
            try:
                async with session.delete(f"{MORPHEUS_URL}/{name}") as _:
                    pass  # 204 = deleted, 404 = already gone — both fine
            except Exception:
                pass
        logger.info("[switch] morpheus cleanup attempted for %d files", len(morpheus_files))

        # 4. Reset stream-lens buffer
        if OUTPUT_URL:
            try:
                async with session.post(
                    f"{OUTPUT_URL}/config",
                    json={"BUFFER_SIZE": config.get_int("BUFFER_SIZE", 7)},
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    logger.info("[switch] stream-lens reset: HTTP %s", resp.status)
            except Exception as exc:
                logger.warning("[switch] stream-lens reset failed: %s", exc)

    logger.info("[switch] done — new source: %s", new_video or "synthetic SMPTE bars")
    return web.json_response(
        {"ok": True, "config": {"LIVE_SIM_VIDEO": _live_sim_video}},
        headers=CORS_HEADERS,
    )


async def handle_media_list(_request: web.Request) -> web.Response:
    """List MP4 files available in /media (the mounted host media directory)."""
    media_dir = Path("/media")
    files: list[str] = []
    if media_dir.is_dir():
        files = sorted(
            f.name for f in media_dir.iterdir()
            if f.is_file() and f.suffix.lower() == ".mp4"
        )
    return web.json_response({"files": files}, headers=CORS_HEADERS)


async def handle_hello(_request: web.Request) -> web.Response:
    return web.Response(text="goodbye", headers=CORS_HEADERS)


async def handle_options(request: web.Request) -> web.Response:
    return web.Response(status=204, headers=CORS_HEADERS)


# ── App wiring ────────────────────────────────────────────────────────────────

async def on_startup(app: web.Application):
    app["patcher"] = asyncio.create_task(patcher_loop())
    if OUTPUT_URL:
        app["pusher"] = asyncio.create_task(segment_pusher_loop())
    logger.info("[server] listening on http://localhost:%s", SERVER_PORT)
    logger.info("[server] player manifest: http://localhost:%s/live.mpd", SERVER_PORT)
    logger.info("[server] output: %s (mpd → %s, segments → %s)", OUTPUT_URL, MPD_PUSH_URL, SEGMENT_PUSH_URL)


async def on_cleanup(app: web.Application):
    app["patcher"].cancel()
    if "pusher" in app:
        app["pusher"].cancel()
    if ffmpeg_proc is not None and ffmpeg_proc.returncode is None:
        ffmpeg_proc.send_signal(signal.SIGTERM)


def make_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/",              handle_index)
    app.router.add_get("/live_scte35.mpd", handle_scte35_mpd)
    app.router.add_get("/segments/{filename}", handle_segment)
    app.router.add_post("/api/start",    handle_start)
    app.router.add_post("/api/stop",     handle_stop)
    app.router.add_post("/api/config",   handle_config)
    app.router.add_get("/api/status",    handle_status)
    app.router.add_get("/api/logs",      handle_logs)
    app.router.add_post("/api/inject",         handle_inject)
    app.router.add_post("/api/inject-overlay", handle_inject_overlay)
    app.router.add_get("/api/list-mpd",        handle_list_mpd)
    app.router.add_get("/api/media",           handle_media_list)
    app.router.add_post("/api/switch",         handle_switch)
    app.router.add_get("/hello",             handle_hello)
    app.router.add_route("OPTIONS", "/{path_info:.*}", handle_options)

    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=SERVER_PORT)
