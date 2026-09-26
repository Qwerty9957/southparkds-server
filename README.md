# SouthparkDS Server

PC-side companion for the `SouthparkDS.nds` homebrew app. Watches for the DSi
requesting `<season>_<episode>.fv`, asks your API for the stream URL, downloads
it, transcodes to 240p, encodes to FastVideoDS `.fv`, caches it, and serves it
back over plain HTTP.

## Files

```
config.example.json <- template for config.json (the one you edit)
server.py          <- the server (Python stdlib + the deps below)
start-server.bat   <- run the server (or use the Desktop shortcut)
stop-server.bat    <- stop the server
ngrok.example.yml  <- template for ngrok.yml (paste YOUR ngrok authtoken)
start-tunnel.bat   <- start the plain-HTTP ngrok tunnel + print its URL
stop-tunnel.bat    <- stop ngrok
get-ngrok-url.py   <- prints the current tunnel URL (called by start-tunnel.bat)
FastVideoDS.nds    <- the player app the DSi can download (served at /FastVideoDS.nds)
wco.py             <- the wco.tv source: mints signed embed tokens, mirrors, retries
sync_ff_cookies.py <- refreshes the wco Firefox profile cookies from a running browser
cache/             <- downloaded+encoded episodes (created automatically)

`config.json`, `ngrok.yml`, the `wco-profile*` browser profiles and `tools/`
are local-only (git-ignored) - copy the `.example` templates to get started and
see "Set-up" below. `tools/` holds the prebuilt FastVideoDSEncoder and player.
```

## Set-up

1. Copy the config template and fill it in:

   ```
   copy config.example.json config.json
   ```

   Open `config.json` and replace the placeholder API url:

   ```json
   "api": {
     "url": "https://PASTE_YOUR_API_URL_HERE.example.com/",
     "tmdbId": "76479",
     "type": "tv",
     ...
   }
   ```

   Your API must answer a GET like

   ```
   <url>?tmdbId=76479&type=tv&season=1&episode=1
   ```

   with a JSON array of at least one object containing a `stream` field, e.g.

   ```json
   [ { "name": "", "image": "", "mediaId": "996", "stream": "https://...m3u8" } ]
   ```

   If your API expects different query parameters, edit `api.queryTemplate`.
   Change `tmdbId` to the show you want, and adjust the episode list under
   `index.seasons` (use `"max": N` for seasons 1..N, or list `episodes`
   explicitly).

2. Double-click **SouthparkDS Server** on the Desktop (or run `start-server.bat`).
   A console shows the LAN address, e.g. `http://10.0.0.39/`.

3. To reach the DSi from outside your LAN, copy the ngrok template and add your token:

   ```
   copy ngrok.example.yml ngrok.yml
   ```

   Replace `ADD_YOUR_NGROK_AUTHTOKEN_HERE` with your token from
   https://dashboard.ngrok.com/get-started/your-authtoken, then run
   `start-tunnel.bat`. It prints the plain-HTTP address, e.g.
   `http://abcd1234.ngrok-free.app/`. (The tunnel is intentionally forced to
   HTTP only - the DSi has no TLS and an https redirect would break it. For a
   stable URL, reserve a domain in the ngrok dashboard and put
   `url: http://yourname.ngrok-free.app` in `ngrok.yml`.)

4. In the DSi app, set the host/port once via **Server URL...** in the in-app
   menu (persisted on the SD card, no rebuild needed - see
   `C:\Users\camer\SouthparkDS\README.md`). The app checks `/health` on every
   boot, fetches `/index.json`, and requests `/1_2.fv`, `/3_5.fv`, etc.

5. Stop with the **SouthparkDS Server - Stop** shortcut or `stop-server.bat`
   (and `stop-tunnel.bat` for ngrok).

## Dependencies

The server uses Python's stdlib for HTTP, but the `wco` source needs two
libs plus local tools:

```
pip install playwright curl_cffi
playwright install firefox
```

- `server.py` shells out to **ffmpeg** and the **FastVideoDSEncoder** (paths in
  `config.json`). The encoder lives in `tools/pub-enc` (from
  `Gericom/FastVideoDSEncoder`, needs the .NET runtime).
- `wco.py` opens the episode page in a persistent **Firefox profile**
  (`wco-profile-ff`) to mint the signed embed token, then streams the actual
  video over plain HTTP with **curl_cffi** (TLS impersonation). That profile
  needs working wco.tv session cookies - refresh them from your logged-in
  browser with `sync_ff_cookies.py` between seasons if embed minting fails.
- Everything here was developed on Windows with Python 3.13.

## Where is the config file?

- `C:\Users\camer\SouthparkDS-server\config.json` - server questions + your API URL.
- `C:\Users\camer\SouthparkDS-server\ngrok.yml` - tunnel authtoken / domain.
- `C:\Users\camer\SouthparkDS\source\config.h` - DS build-time defaults (LAN IP,
  port, URL prefix) used when no SD config exists.
- On the SD card: `SouthPark/config.txt` - the runtime server URL the DSi app
  actually uses (edited from the in-app **Server URL...** screen).

## Notes

- Plain HTTP only (the DS has no TLS). Keep the server on your own LAN, or
  expose it through the included ngrok tunnel.
- Downloads + encoding of a full episode takes a while (ffmpeg pull + lossless-ish
  FastVideoDS encode). Episodes are cached, so a second request is instant.
- If Windows Firewall asks, allow Python on Private networks so the DSi can reach
  the server.
- The first request triggered by the DSi for an uncached episode triggers the
  full pipeline and can take several minutes; the DSi waits while ffmpeg+encoder
  run.

## Toolchain pieces (already installed during setup)

- Python 3.13 (`C:\Users\camer\AppData\Local\Programs\Python\Python313\`)
- ffmpeg via winget (`Gyan.FFmpeg`, 9.0.2 full build)
- FastVideoDSEncoder (built from `Gericom/FastVideoDSEncoder` with a local
  .NET 6 SDK; net6.0 framework-dependent, runs on the installed 6.0.36 runtime)

## Building the DSi app

```
cd C:\Users\camer\SouthparkDS && make
```
(or the WSL->Windows make one-liner from `SouthparkDS/README.md`).