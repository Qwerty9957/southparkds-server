"""SouthparkDS server.

Listens for requests from the homebrew DSi app:

  GET /index.json         -> generated episode index (from config.json)
  GET /<S>_<E>.fv         -> if not cached: grab the episode from wco.tv,
                             transcode to 240p with ffmpeg, encode to
                             FastVideoDS .fv, cache it, then serve it.

Plain HTTP only (the DS has no TLS). wco.tv (see wco.py) is the sole
episode source.
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time as _time
import urllib.request
import html as _html
import glob as _glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CONFIG = json.load(fh)

VID = CONFIG["video"]
CACHE = os.path.join(BASE_DIR, CONFIG.get("cacheDir", "cache"))
PLAYER = os.path.join(BASE_DIR, "FastVideoDS.nds")
WCO_CFG = CONFIG.get("wco", {}) or {}
WCO_ENABLED = bool(WCO_CFG.get("enabled")) and os.path.exists(
    os.path.join(BASE_DIR, "wco.py"))

BUILD_LOCK = threading.Lock()

# background-build status so the DSi can show live progress text. Keyed
# by "S_E": {"state": "queued"|"downloading"|"converting"|"ready"|"error",
#            "message": str}
BUILD_STATE_LOCK = threading.Lock()
BUILD_STATUS = {}
BUILDING = set()   # "S_E" keys whose build is running in a background thread


def set_build_state(season, episode, state, message=""):
    with BUILD_STATE_LOCK:
        BUILD_STATUS["%d_%d" % (season, episode)] = {
            "state": state, "message": message}


def build_state(season, episode):
    key = "%d_%d" % (season, episode)
    if os.path.exists(os.path.join(CACHE, key + ".fv")):
        return {"state": "ready", "message": ""}
    with BUILD_STATE_LOCK:
        return BUILD_STATUS.get(key) or {"state": "queued", "message": ""}


def log(msg):
    print("[%s] %s" % (time_str(), str(msg)), flush=True)


def time_str():
    return _time.strftime("%H:%M:%S")


def build_index():
    out = []
    for item in CONFIG["index"]["seasons"]:
        season = int(item["season"])
        if "episodes" in item:
            eps = [int(e) for e in item["episodes"]]
        else:
            eps = list(range(1, int(item.get("max", 13)) + 1))
        out.append({"season": season, "episodes": eps})
    return {"seasons": out}


def _cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


TITLES_CACHE = os.path.join(BASE_DIR, "episode_titles.json")


def load_episode_titles():
    """Episode titles by season (1-indexed list), fetched from the English
    Wikipedia season pages and cached to episode_titles.json."""
    if os.path.exists(TITLES_CACHE):
        with open(TITLES_CACHE, encoding="utf-8") as fh:
            return json.load(fh)

    pat = re.compile(r'"Title"\s*:\s*\{\s*"wt"\s*:\s*"([^"]+)"')
    titles = {}
    for item in CONFIG["index"]["seasons"]:
        season = int(item["season"])
        url = "https://en.wikipedia.org/wiki/South_Park_(season_%d)" % season
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SouthparkDS/1.0"})
            h = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        except Exception:
            log("Wikipedia fetch failed for season %d (using SxEy search only)" % season)
            continue
        out = []
        for m in pat.finditer(h):
            t = _html.unescape(m.group(1)).strip()
            t = re.sub(r'^\[\[|\]\]$', "", t)
            t = t.split("|")[-1].strip()
            if not t or re.match(r"(?i)^(episode|list of)\b", t):
                continue
            out.append(t)
        if out:
            titles[str(season)] = out
            log("Wikipedia: season %d -> %d episode titles" % (season, len(out)))

    try:
        with open(TITLES_CACHE, "w", encoding="utf-8") as fh:
            json.dump(titles, fh, ensure_ascii=False)
    except OSError:
        pass
    return titles


def episode_title(season, episode):
    s = load_episode_titles().get(str(season))
    if s and 1 <= episode <= len(s):
        return s[episode - 1]
    return None


def _wco_source(season, episode, tmp_src):
    """Download the episode from wco.tv to tmp_src.
    Returns '' on success or an error string."""
    if not WCO_ENABLED:
        return "wco source disabled (set 'wco.enabled' = true in config.json)"
    try:
        import wco
        profile = WCO_CFG.get("profile") or "wco-profile-ff"
        wco.init(headed=bool(WCO_CFG.get("headed")),
                 retries=WCO_CFG.get("retries", 2),
                 proxy=WCO_CFG.get("proxy"),
                 profile=os.path.join(BASE_DIR, profile),
                 log=log)
        wco.wco_download(season, episode,
                         episode_title(season, episode),
                         tmp_src)
        log("wco: downloaded %d_%d (%d MB)"
            % (season, episode, os.path.getsize(tmp_src) // 1024 // 1024))
        return ""
    except Exception as ex:
        log("wco source failed for %d_%d: %s" % (season, episode, str(ex)[:500]))
        _cleanup(tmp_src)
        return "wco: %s" % ex


def _probe_audio_codec(src, ffmpeg):
    """Return the source's audio codec name (e.g. 'aac') or '' on failure."""
    try:
        ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe")
        p = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
             src, ],
            capture_output=True, text=True, timeout=60)
        return (p.stdout.strip() or "").strip()
    except Exception:
        return ""


def build_file(season, episode, out_path):
    """Download -> transcode 240p -> encode .fv. Returns error string or ''.
    Publishes build state so GET /<S>_<E>.json reports progress."""
    set_build_state(season, episode, "downloading",
                    "fetching episode from wco.tv")
    err = _build_file_inner(season, episode, out_path)
    if err:
        set_build_state(season, episode, "error", err)
    else:
        set_build_state(season, episode, "ready", "")
    return err


def _build_file_inner(season, episode, out_path):
    """Download -> transcode 240p -> encode .fv. Returns error string or ''."""
    tmp_mp4 = out_path + ".tmp.mp4"
    tmp_src = out_path + ".src.mp4"
    try:
        err = _wco_source(season, episode, tmp_src)
        if err:
            return err
        src = tmp_src

        ffmpeg = CONFIG["ffmpeg"]
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if not os.path.exists(src):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"]
        cmd += [
            "-i", src,
            "-vf", "scale=-2:%d,fps=%d" % (VID["height"], VID["fps"]),
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(VID.get("crf", 24)),
        ]
        # Keep the source audio bit-exact when it's already AAC. Re-encoding at
        # 96k with ffmpeg's native AAC added a ~19 dB noise floor (measured ~18.4 dB
        # SNR vs source) that sounded like heavy distortion on the DSi. Copying
        # removes it entirely; fall back to a high-ish 192k re-encode for other codecs
        # (mp4 can't carry MP3, and the native encoder needs the extra bits).
        audio_codec = _probe_audio_codec(src, ffmpeg)
        if audio_codec == "aac":
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += [tmp_mp4]
        log("ffmpeg: downloading+transcoding (this can take a while for a whole episode)")
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if p.returncode != 0:
            return "ffmpeg failed: " + (p.stderr[-600:] or "no output")

        enc_bat = CONFIG["encoder"]
        enc_dir = os.path.dirname(enc_bat)
        enc_exe = os.path.join(enc_dir, "FastVideoDSEncoder.exe")
        if not os.path.exists(enc_exe):
            return "encoder missing: %s" % enc_exe
        enc_out = tmp_mp4 + ".fv"
        jobs = VID.get("jobs", 1)
        set_build_state(season, episode, "converting",
                        "encoding FastVideoDS .fv")
        log("encoder: FastVideoDS (~%s jobs) -> %s"
            % (jobs, os.path.basename(enc_out)))
        # The .NET encoder needs a real console (it calls
        # Console.GetBufferInfo) or it crashes with "The handle is invalid".
        # 'cmd /c start' gives it its own console, so it works even when the
        # server itself has none attached. Its output goes to that window
        # (not captured); success is detected by enc_out existing below.
        cmd2 = ["cmd", "/c", "start", "", "/wait", "/D", enc_dir,
                enc_exe, "-j", str(jobs), tmp_mp4, enc_out]
        p2 = subprocess.run(cmd2, cwd=enc_dir, stdin=subprocess.DEVNULL,
                            stdout=None, stderr=None, timeout=3600)
        if p2.returncode != 0 or not os.path.exists(enc_out):
            detail = ""
            try:
                detail = (p2.stderr or p2.stdout or "")[-600:]
            except AttributeError:
                pass
            return "encoder failed: " + (detail or "no output, exit=%s"
                                          % p2.returncode)
        os.replace(enc_out, out_path)
        log("done: %s (%d KB)" % (os.path.basename(out_path),
                                  os.path.getsize(out_path) // 1024))
        return ""
    except Exception as ex:  # noqa: BLE001 - surface any failure to the DSi
        return "build error: %s" % ex
    finally:
        for leftover in _glob.glob(tmp_mp4 + "*"):
            _cleanup(leftover)
        for leftover in _glob.glob(tmp_src + "*"):
            _cleanup(leftover)


def ensure_build(season, episode, out_path):
    """Start a background build for an uncached episode (idempotent).
    Returns immediately with the current state."""
    if os.path.exists(out_path):
        return {"state": "ready", "message": ""}
    key = "%d_%d" % (season, episode)
    with BUILD_STATE_LOCK:
        if key in BUILDING:
            return BUILD_STATUS.get(key) or {"state": "queued", "message": ""}
        BUILDING.add(key)
        BUILD_STATUS[key] = {"state": "queued", "message": ""}
    threading.Thread(target=_background_build, args=(season, episode, out_path),
                     daemon=True).start()
    return BUILD_STATUS[key]


def _background_build(season, episode, out_path):
    key = "%d_%d" % (season, episode)
    try:
        # a foreground request may also be building; BUILD_LOCK serializes
        with BUILD_LOCK:
            if not os.path.exists(out_path):
                build_file(season, episode, out_path)
    except Exception as ex:  # noqa: BLE001
        set_build_state(season, episode, "error", str(ex))
    finally:
        with BUILD_STATE_LOCK:
            BUILDING.discard(key)


class Handler(BaseHTTPRequestHandler):
    server_version = "SouthparkDS/1.0"

    def log_message(self, fmt, *args):
        log("HTTP %s" % (fmt % args))

    def _send_bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path in ("/", "/health"):
            return self._send_bytes(200, b"ok", "text/plain")

        if path == "/index.json":
            return self._send_bytes(
                200,
                json.dumps(build_index()).encode(),
                "application/json",
            )

        if path == "/FastVideoDS.nds":
            if not os.path.exists(PLAYER):
                return self._send_bytes(
                    404, b"player not installed on server", "text/plain")
            return self._send_file(PLAYER)

        m = re.fullmatch(r"/(\d+)_(\d+)\.json", path)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
            out = os.path.join(CACHE, "%d_%d.fv" % (season, episode))
            ensure_build(season, episode, out)
            st = build_state(season, episode)
            return self._send_bytes(
                200,
                json.dumps({"state": st["state"], "message": st["message"]}).encode(),
                "application/json",
            )

        m = re.fullmatch(r"/(\d+)_(\d+)\.fv", path)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
            return self._serve_or_build(season, episode)

        return self._send_bytes(404, b"not found", "text/plain")

    def _serve_or_build(self, season, episode):
        os.makedirs(CACHE, exist_ok=True)
        out = os.path.join(CACHE, "%d_%d.fv" % (season, episode))
        if not os.path.exists(out):
            with BUILD_LOCK:
                if not os.path.exists(out):
                    err = build_file(season, episode, out)
                    if err:
                        return self._send_bytes(
                            502, ("bad gateway: %s" % err).encode(),
                            "text/plain")
        return self._send_file(out)


def main():
    os.makedirs(CACHE, exist_ok=True)
    pid = os.path.join(BASE_DIR, "server.pid")
    with open(pid, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    port = int(CONFIG.get("port", 8080))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)

    if WCO_ENABLED:
        # keep the trusted Firefox copy in step with the live profile's
        # Cloudflare/session cookies; closes Firefox if it is open
        try:
            import sync_ff_cookies as _sync
            print("  wco cookies     : %s" % _sync.sync_cookies(), flush=True)
        except Exception as ex:  # noqa: BLE001
            print("  wco cookies     : resync failed (%s)" % ex, flush=True)

    print("=" * 56, flush=True)
    print("SouthparkDS server running.")
    if WCO_ENABLED:
        print("  Source         : wco.tv (browser-minted embed token + curl_cffi)")
    else:
        print("  Source         : wco.tv DISABLED (set wco.enabled=true in config.json)")
    print("  Listen         : port %d on all interfaces" % port)
    print("  Cache dir      : %s" % CACHE)
    try:
        ip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ip.connect(("8.8.8.8", 80))
        print("  Point homebrew APP at  : http://%s/  (SSID WiFi)" % ip.getsockname()[0])
        ip.close()
    except OSError:
        pass
    print("  Stop with stop-server.bat or close this window.", flush=True)
    print("=" * 56, flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup(pid)


if __name__ == "__main__":
    sys.exit(main())