"""wco.tv / wcostream.tv episode source (primary).

The wco family gates videos behind Cloudflare, a signed embed token
(h/t), and an interstitial ad page. A plain HTTP client (curl_cffi with a
real-browser TLS impersonation) is enough for everything *after* the
episode page; the only browser-only step is minting the signed embed URL,
which needs a Cloudflare-cleared session (the user's trusted Firefox
profile copy, wco-profile-ff).

  steps per attempt:
    1. browser (Playwright Firefox + trusted profile) opens the episode
       page and reads the signed embed iframe URL
       (embed.wcostream.com/inc/embed/index.php?file=..&h=..&t=..)
    2. curl_cffi fetches the player page video-js.php?<query>; from it we
       regex the getvidlink URL built server-side
    3. curl_cffi fetches getvidlink.php?v=.. -> JSON {server, enc, ..}
    4. curl_cffi fetches {server}/getvid?evid=<enc>&json -> JSON string of
       the final streaming node (e.g. https://u21.wcostream.com/getvid?evid=..)
    5. the media node is streamed down with curl_cffi. MP4, verified.

The browser is closed once the embed token is captured, so only the tiny
discovery traffic ever goes through a real browser. All big-file traffic
is curl_cffi via our own IP.

Robustness layers:
  * Multiple mirror hostnames are tried in turn (they share one
    embed/player host, so mirrors mainly survive domain-level outages).
  * Optional proxy for the browser step only (hybrid mode): the tiny
    token/cookie traffic can go out a rotating residential IP, the media
    file downloads from our own IP. Use a *sticky-session* endpoint so one
    episode keeps a single exit IP (the signed tokens are IP-bound).
"""

import json
import os
import re
import time

from playwright.sync_api import sync_playwright

try:
    from curl_cffi import requests as _cr
except Exception:  # noqa: BLE001 - translated to a clear error on use
    _cr = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""

# Mirror hostnames, fastest/most reliable first. wco.tv is the one our
# trusted Firefox profile has a Cloudflare clearance on, so it is tried
# first; the others share the same site/embed backend. Verified live.
SITES = ["wco.tv", "wcostream.tv", "wcoforever.net",
         "watchcartoononline.io", "watchcartoononline.cc",
         "wcostream.one", "wcostream.net"]

HEADED = False
RETRIES = 2
PROXY = None
PROFILE = None
LOG = print

# best-effort memory of the last mirror that worked for us
_PREFERRED_SITE = None

# canonical episode-URL cache, scraped from each mirror's series page and
# persisted next to this file: {"host": {season: {episode: full_url}}}
_EP_CACHE = {}
_EP_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "wco_episode_urls.json")


def _cache_load():
    try:
        with open(_EP_CACHE_PATH, encoding="utf-8") as fh:
            _EP_CACHE.update(json.load(fh))
    except Exception:
        pass


def _cache_save():
    try:
        with open(_EP_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_EP_CACHE, fh)
    except Exception:
        pass


def series_url(host):
    return "https://www.%s/anime/south-park/?season=all&lang=cartoon" % host


def _default_profile():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "wco-profile-ff")


def init(headed=False, retries=2, proxy=None, profile=None, log=print):
    """proxy: None, or dict like
    {"server": "http://user:pass@host:port"}
    or {"server": "socks5://host:port", "username": "..", "password": ".."}.
    Use a sticky-session endpoint so one episode keeps a single exit IP.
    profile: path to a trusted Firefox profile (Cloudflare clearance +
    embedding session). Defaults to ./wco-profile-ff next to this file."""
    global HEADED, RETRIES, PROXY, PROFILE, LOG
    HEADED = bool(headed)
    RETRIES = max(1, int(retries))
    PROXY = proxy if isinstance(proxy, dict) else None
    PROFILE = profile or _default_profile()
    LOG = log
    _cache_load()


def _slugify(title):
    t = title.lower()
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")


def episode_url(host, season, episode, title=None):
    slug = _slugify(title) if title else ""
    if slug:
        return "https://www.%s/south-park-season-%d-episode-%d-%s" % (
            host, season, episode, slug)
    return "https://www.%s/south-park-season-%d-episode-%d" % (host, season, episode)


def _browser_launch_kwargs():
    kw = {"headless": not HEADED}
    if PROXY:
        p = {"server": PROXY.get("server")}
        if PROXY.get("username"):
            p["username"] = PROXY["username"]
            p["password"] = PROXY.get("password") or ""
        kw["proxy"] = p
    return kw


def _find_embed(page):
    try:
        loc = page.locator('iframe[src*="index.php"]')
        loc.first.wait_for(timeout=35000)
        src = loc.first.get_attribute("src")
        if src:
            return src
    except Exception:
        pass
    for m in re.finditer(r'<iframe[^>]+src="([^"]+)"', page.content(), re.I):
        if "index.php" in m.group(1) and "embed" in m.group(1):
            return m.group(1)
    return None


def _not_404(page):
    """False on a '404 - Page Not Found' page so we skip the long embed
    wait and go straight to the canonical-URL scrape."""
    try:
        return "404" not in page.title()
    except Exception:
        return True


def _scoped_cookies(ctx, host):
    """Cookies the embed/player/media hosts and the mirror actually see."""
    want = ("wcostream.com", host, "wco.tv")
    out = {}
    try:
        for c in ctx.cookies():
            d = str(c.get("domain") or "").lstrip(".")
            if any(w in d for w in want):
                if c.get("expires") and c["expires"] < time.time():
                    continue
                out[c["name"]] = c["value"]
    except Exception:
        pass
    return out


def _canonical_url(host, season, episode, title):
    """Known canonical URL for (host, season, episode), else a best-effort
    slug from the title (fallback only)."""
    try:
        url = _EP_CACHE[host][str(season)][str(episode)]
        if url:
            return url
    except (KeyError, TypeError):
        pass
    return episode_url(host, season, episode, title)


def _scrape_episode_map(page, host, season):
    """Open the mirror's South Park series page and collect the canonical
    URL for every episode of `season`. Returns {episode: full_url} (also
    cached on disk)."""
    try:
        page.goto(series_url(host), wait_until="domcontentloaded",
                  timeout=60000)
    except Exception:
        return {}
    hrefs = page.eval_on_selector_all("a", "es => es.map(e => e.href)")
    m = {}
    for h in hrefs:
        mm = re.search(r"/south-park-season-%d-episode-(\d+)-" % season, h)
        if mm:
            m[int(mm.group(1))] = h
    if m:
        _EP_CACHE.setdefault(host, {})[str(season)] = {
            str(k): v for k, v in m.items()}
        _cache_save()
    return m


def _get_embed(host, season, episode, title):
    """Browser-only step: return (embed_url, cookies) or (None, {})."""
    profile = PROFILE or _default_profile()
    if not os.path.isdir(profile):
        LOG("wco: trusted Firefox profile missing at %s" % profile)
        return None, {}
    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(
            profile, **_browser_launch_kwargs(),
            viewport={"width": 1280, "height": 720})
        try:
            ctx.add_init_script(STEALTH)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ep_url = _canonical_url(host, season, episode, title)
            page.goto(ep_url, wait_until="domcontentloaded", timeout=60000)
            embed = _find_embed(page) if _not_404(page) else None
            if not embed:
                # unknown slug (e.g. title mismatch) - scrape the real
                # series map for this host/season and try again once
                mapped = _scrape_episode_map(page, host, season)
                c = mapped.get(episode)
                if c:
                    ep_url = c
                    page.goto(c, wait_until="domcontentloaded",
                              timeout=60000)
                    embed = _find_embed(page) if _not_404(page) else None
            cookies = _scoped_cookies(ctx, host)
            if not embed:
                try:
                    t = page.title()
                except Exception:
                    t = "(no title)"
                LOG("wco: no embed iframe on %s (%s; title=%r)"
                    % (ep_url, "Cloudflare/rate-limit?" if "404" not in t
                       else "404 not found", t))
            return embed, cookies
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def _curl_step(sess, url, headers, referer, cookies):
    h = dict(headers)
    h["Referer"] = referer
    resp = sess.get(url, headers=h, timeout=(20, 60))
    if resp.status_code >= 400:
        raise RuntimeError("wco: HTTP %s from %s" % (resp.status_code, url[:80]))
    return resp


def _curl_resolve(embed_url, ep_url, cookies):
    """Steps 2-4, all plain HTTP. Returns (referer, media_url, cookies)."""
    embed_host = re.match(r"https?://([^/]+)", embed_url).group(1)
    query = embed_url.split("?", 1)[1]
    base = "https://%s/inc/embed" % embed_host
    player_url = "%s/video-js.php?%s" % (base, query)

    sess = _cr.Session(impersonate="firefox133")
    if cookies:
        sess.cookies.update(cookies)

    p = _curl_step(sess, player_url,
                   {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "cross-site"},
                   ep_url, cookies)
    m = re.search(r'getvidlink\.php\?v=([^"\']+)', p.text)
    if not m:
        raise RuntimeError("wco: no getvidlink URL in player page")
    gv = "%s/getvidlink.php?v=%s" % (base, m.group(1).replace("&amp;", "&"))

    j = _curl_step(sess, gv,
                   {"Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest"},
                   player_url, cookies)
    data = j.json()
    server = data.get("server")
    enc = data.get("enc")
    if not server or not enc:
        raise RuntimeError("wco: getvidlink response missing server/enc")

    n = _curl_step(sess, "%s/getvid?evid=%s&json" % (server, enc),
                   {"Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest"},
                   player_url, cookies)
    media_url = json.loads(n.text)
    LOG("wco: media resolved -> %s" % media_url[:90])
    return player_url, media_url, cookies


def resolve_media_url(season, episode, title=None):
    """Return (referer, media_url, cookies) or raise RuntimeError.
    Tries each mirror hostname; the one that yields media becomes the
    preferred site for subsequent requests."""
    global _PREFERRED_SITE
    for attempt in range(1, RETRIES + 1):
        if attempt > 1:
            cool = 45 + attempt * 20
            LOG("wco: attempt %d of %d failed; cooling down %ds..."
                % (attempt - 1, RETRIES, cool))
            time.sleep(cool)
        for host in _site_order():
            ep_url = episode_url(host, season, episode, title)
            try:
                LOG("wco: trying %s" % ep_url)
                embed, cookies = _get_embed(host, season, episode, title)
                if not embed or not cookies:
                    continue
                LOG("wco: embed token acquired (pid/t/h ok)")
                referer, media_url, cookies = _curl_resolve(
                    embed, ep_url, cookies)
                _PREFERRED_SITE = host
                return referer, media_url, cookies
            except Exception as ex:
                LOG("wco: %s failed: %s" % (host, str(ex)[:200]))
    raise RuntimeError(
        "wco: could not resolve a media URL for %d_%d" % (season, episode))


def _site_order():
    order = [_PREFERRED_SITE] if _PREFERRED_SITE else []
    order += [s for s in SITES if s != _PREFERRED_SITE]
    return order


def _cleanup(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def download_media(url, referer, cookies, out_mp4):
    if _cr is None:
        raise RuntimeError(
            "wco: curl_cffi is required for downloads "
            "(pip install curl_cffi)")
    sess = _cr.Session(impersonate="firefox133")
    if cookies:
        sess.cookies.update(cookies)
    headers = {
        "Accept": "*/*",
        "Referer": referer,
        "Accept-Language": "en-US,en;q=0.9",
    }
    LOG("wco: downloading %s" % url[:110])
    size = 0
    try:
        with sess.stream("GET", url, headers=headers, timeout=(30, 900)) as resp:
            if resp.status_code >= 400:
                raise RuntimeError("wco: media HTTP %s" % resp.status_code)
            with open(out_mp4, "wb") as fh:
                for chunk in resp.iter_content(1 << 16):
                    fh.write(chunk)
                    size += len(chunk)
    except Exception:
        _cleanup(out_mp4)
        raise
    if size == 0:
        _cleanup(out_mp4)
        raise RuntimeError("wco: empty download")
    LOG("wco: got %d MB" % (size // 1024 // 1024))


def wco_download(season, episode, title, out_mp4):
    referer, url, cookies = resolve_media_url(season, episode, title)
    download_media(url, referer, cookies, out_mp4)