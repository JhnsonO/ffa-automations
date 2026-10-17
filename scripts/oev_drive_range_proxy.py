"""Serve /<drive_file_id> on 127.0.0.1:<port> by forwarding ranged GETs to Drive.

OEV-006 P1: ffmpeg cuts fixed windows from multi-GB Drive sources without
downloading them. Drive's download endpoint only returns media for ranged
requests (an unranged GET returns an HTML interstitial), and ffmpeg's first
request is unranged, so this proxy turns every request into a bounded
Range request (<= 64 MiB per response; ffmpeg re-requests as it reads).

Usage: python3 scripts/oev_drive_range_proxy.py 8765 &
       ffmpeg -ss 1000 -i http://127.0.0.1:8765/<file_id> -t 300 -c copy out.mp4
"""
import http.server
import os
import re
import socketserver
import ssl
import sys
import urllib.request

CA = os.environ.get("SSL_CERT_FILE")
CTX = ssl.create_default_context(cafile=CA) if CA else ssl.create_default_context()
SIZES = {}
def url(fid): return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
def fetch(fid, start, end):
    req = urllib.request.Request(url(fid), headers={"Range": f"bytes={start}-{end}", "User-Agent": "oev-range-proxy"})
    r = urllib.request.urlopen(req, context=CTX, timeout=120)
    total = int(r.headers["Content-Range"].split("/")[1])
    return r, total
CHUNK = 64 * 1024 * 1024


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.handle_get(head=True)

    def do_GET(self):
        self.handle_get()

    def handle_get(self, head=False):
        fid = self.path.strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", fid):
            self.send_error(404)
            return
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", "bytes=0-"))
        start = int(m.group(1)) if m else 0
        if fid not in SIZES:
            r, SIZES[fid] = fetch(fid, 0, 0)
            r.read()
        total = SIZES[fid]
        end = int(m.group(2)) if m and m.group(2) else total - 1
        end = min(end, total - 1)
        self.send_response(206)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if head:
            return
        pos = start
        while pos <= end:
            piece_end = min(pos + CHUNK - 1, end)
            r, _ = fetch(fid, pos, piece_end)
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                pos += len(chunk)
            if pos <= piece_end:
                return  # upstream ended early; let the client re-request


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    S(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
