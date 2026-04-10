"""
Lightweight reset API server for browsergym-human-recorder.

Runs on the server alongside the Mattermost Docker container.
Exposes a POST /reset endpoint that restarts the container.

Usage:
    python reset_server.py [--port 5000] [--api-key YOUR_KEY]

The API key can also be set via the RESET_API_KEY environment variable.
"""

import argparse
import os
import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

MATTERMOST_IMAGE = "mattermost-populated"
MATTERMOST_CONTAINER = "mattermost"
MATTERMOST_PORT = 8065

API_KEY = None


def _docker_cmd():
    """Return docker command, using sudo if needed."""
    result = subprocess.run("docker info", shell=True, capture_output=True)
    if result.returncode == 0:
        return "docker"
    result = subprocess.run("sudo docker info", shell=True, capture_output=True)
    if result.returncode == 0:
        return "sudo docker"
    return "docker"


DOCKER = _docker_cmd()


def reset_mattermost():
    """Stop, remove, and restart the Mattermost container."""
    subprocess.run(f"{DOCKER} rm -f {MATTERMOST_CONTAINER}", shell=True, capture_output=True)
    result = subprocess.run(
        f"{DOCKER} run -d --name {MATTERMOST_CONTAINER}"
        f" -p {MATTERMOST_PORT}:{MATTERMOST_PORT} {MATTERMOST_IMAGE}",
        shell=True, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip()
    # Wait for Mattermost to be ready
    time.sleep(5)
    return True, "Mattermost reset successfully."


class ResetHandler(BaseHTTPRequestHandler):
    def _send_json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_POST(self):
        if self.path != "/reset":
            self._send_json(404, {"error": "Not found"})
            return

        # Check API key
        if API_KEY:
            auth = self.headers.get("X-API-Key", "")
            if auth != API_KEY:
                self._send_json(401, {"error": "Invalid API key"})
                return

        print("[reset] Resetting Mattermost container...")
        ok, msg = reset_mattermost()
        if ok:
            print(f"[reset] {msg}")
            self._send_json(200, {"status": "ok", "message": msg})
        else:
            print(f"[reset] Failed: {msg}")
            self._send_json(500, {"status": "error", "message": msg})

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        # Quieter logging
        pass


def main():
    global API_KEY
    parser = argparse.ArgumentParser(description="Reset API server for Mattermost")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--api-key", type=str, default=None)
    args = parser.parse_args()

    API_KEY = args.api_key or os.environ.get("RESET_API_KEY")
    if not API_KEY:
        print("[warn] No API key set. The reset endpoint is unprotected.")
        print("       Use --api-key or set RESET_API_KEY environment variable.")

    server = HTTPServer(("0.0.0.0", args.port), ResetHandler)
    print(f"[reset-server] Listening on port {args.port}")
    if API_KEY:
        print(f"[reset-server] API key: {API_KEY}")
    server.serve_forever()


if __name__ == "__main__":
    main()
