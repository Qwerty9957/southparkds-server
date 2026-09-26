"""Print the current public ngrok URL for the SouthparkDS DSi app.

Run after start-tunnel.bat. Queries the ngrok local API (default 127.0.0.1:4040).
"""

import json
import sys
import time
import urllib.request


def main():
    deadline = time.time() + 25
    last = "ngrok API not reachable yet"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:4040/api/tunnels", timeout=2
            ) as resp:
                data = json.load(resp)
            http_urls = [
                t.get("public_url", "")
                for t in data.get("tunnels", [])
                if t.get("public_url", "").startswith("http://")
            ]
            if http_urls:
                print("Public URL for the DSi app (plain HTTP):")
                print("  " + http_urls[0] + "/")
                print("Enter that in the DSi in-app setting (Menu -> Server URL).")
                return 0
            last = "tunnel exists but has no plain-HTTP endpoint yet"
        except Exception as ex:  # noqa: BLE001
            last = "ngrok API not reachable: %s" % ex
        time.sleep(1)

    print(
        "Could not get the tunnel URL: %s\n"
        "Make sure ngrok is running and ngrok.yml has your authtoken." % last,
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())