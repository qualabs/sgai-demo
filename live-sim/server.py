#!/usr/bin/env python3
"""
Live-Sim: DASH live streaming simulator with SCTE-35 event injection.

Flow:
  FFmpeg (loops bbb_320x180.mp4) → segments + live.mpd (dynamic)
  Patcher loop (every 2s): read live.mpd + add BaseURL + inject SCTE-35 → PUT to Morpheus
  Morpheus (port 80): SCTE-35 → <ReplacePresentation> → serves patched MPD
  GET /live.mpd: proxy from Morpheus → player
  GET /segments/*: serve FFmpeg segments
  GET /api/list-mpd: SGAI endpoint → alternative content MPD
"""

import asyncio
import collections
import io
import json
import logging
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

MORPHEUS_URL     = config.get("MORPHEUS_URL", "http://morpheus")
MORPHEUS_GET_URL = f"{MORPHEUS_URL}/live.mpd"
MORPHEUS_PUT_URL = f"{MORPHEUS_GET_URL}?mode=scte-to-alternative"
VAST2SGAI_URL    = config.get("VAST2SGAI_URL", "http://localhost:3000")
SERVER_PORT      = config.get_int("SERVER_PORT", 8000)
PATCH_INTERVAL   = config.get_int("PATCH_INTERVAL", 2)


FFMPEG_CMD = [
    "ffmpeg",
    "-re",
    "-f", "lavfi", "-i", "smptehdbars=size=1920x1080:rate=30",
    "-f", "lavfi", "-i", "aevalsrc=exprs='if(lt(mod(t\\,1)\\,0.08)\\,sin(2*PI*1000*t)\\,0)|if(lt(mod(t\\,1)\\,0.08)\\,sin(2*PI*1000*t)\\,0):sample_rate=48000",
    "-map", "0:v", "-map", "1:a",
    "-vf", (
        "realtime,"
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
    ),
    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
    "-crf", "28", "-g", "60",
    "-pix_fmt", "yuv420p",
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

# ── State ─────────────────────────────────────────────────────────────────────

ffmpeg_proc:    asyncio.subprocess.Process | None = None
ffmpeg_start:   datetime | None = None   # UTC time FFmpeg was launched
pending_events: list[dict]               = []    # active SCTE-35 events
event_counter:  int                      = 0     # local event id counter
ffmpeg_log:     collections.deque        = collections.deque(maxlen=200)

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

        insert = ET.SubElement(section, _scte_ns("SpliceInsert"))
        insert.set("spliceEventId", "1")          # always 1 → Morpheus hardcoded URL
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

    # Add BaseURL so the player can fetch segments from us
    base_url_el = ET.Element(_ns("BaseURL"))
    base_url_el.text = f"http://localhost:{SERVER_PORT}/segments/"
    period.insert(0, base_url_el)

    # Inject SCTE-35 EventStream before AdaptationSets
    if active_events:
        scte_es = build_scte35_event_stream(active_events)
        # Insert after BaseURL (index 1)
        period.insert(1, scte_es)

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
                            MORPHEUS_PUT_URL,
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


async def handle_live_mpd(request: web.Request) -> web.Response:
    """Proxy GET /live.mpd → Morpheus → player."""
    try:
        async with ClientSession(timeout=ClientTimeout(total=5)) as session:
            async with session.get(MORPHEUS_GET_URL) as resp:
                body = await resp.read()
                return web.Response(
                    body=body,
                    content_type="application/dash+xml",
                    headers=CORS_HEADERS,
                )
    except Exception as exc:
        return web.Response(
            status=502,
            text=f"Could not reach Morpheus: {exc}",
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

    if ffmpeg_proc is not None and ffmpeg_proc.returncode is None:
        return web.json_response({"ok": False, "msg": "FFmpeg already running"}, headers=CORS_HEADERS)

    SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)

    # Remove old MPD to avoid stale data
    if MPD_PATH.exists():
        MPD_PATH.unlink()

    ffmpeg_proc = await asyncio.create_subprocess_exec(
        *FFMPEG_CMD,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    ffmpeg_start   = datetime.now(timezone.utc)
    pending_events = []
    event_counter  = 0
    ffmpeg_log.clear()
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
            "id":          e["id"],
            "fire_in_s":   round(fire_in, 1) if fire_in is not None else None,
            "duration_s":  round(e["duration_ms"] / 1000, 1),
            "expires_in_s": round(expires_in, 1),
        })

    return web.json_response(
        {
            "ffmpeg_running": running,
            "ffmpeg_pid":     ffmpeg_proc.pid if running else None,
            "events":         events_info,
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


async def handle_hello(_request: web.Request) -> web.Response:
    return web.Response(text="goodbye", headers=CORS_HEADERS)


async def handle_options(request: web.Request) -> web.Response:
    return web.Response(status=204, headers=CORS_HEADERS)


# ── App wiring ────────────────────────────────────────────────────────────────

async def on_startup(app: web.Application):
    app["patcher"] = asyncio.create_task(patcher_loop())
    logger.info("[server] listening on http://localhost:%s", SERVER_PORT)
    logger.info("[server] player manifest: http://localhost:%s/live.mpd", SERVER_PORT)


async def on_cleanup(app: web.Application):
    app["patcher"].cancel()
    if ffmpeg_proc is not None and ffmpeg_proc.returncode is None:
        ffmpeg_proc.send_signal(signal.SIGTERM)


def make_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/",              handle_index)
    app.router.add_get("/live.mpd",        handle_live_mpd)
    app.router.add_get("/live_scte35.mpd", handle_scte35_mpd)
    app.router.add_get("/segments/{filename}", handle_segment)
    app.router.add_post("/api/start",    handle_start)
    app.router.add_post("/api/stop",     handle_stop)
    app.router.add_get("/api/status",    handle_status)
    app.router.add_get("/api/logs",      handle_logs)
    app.router.add_post("/api/inject",   handle_inject)
    app.router.add_get("/api/list-mpd",      handle_list_mpd)
    app.router.add_get("/hello",             handle_hello)
    app.router.add_route("OPTIONS", "/{path_info:.*}", handle_options)

    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=SERVER_PORT)
