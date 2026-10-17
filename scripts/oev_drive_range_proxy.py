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
import time
import urllib.error
import urllib.request

CA = os.environ.get("SSL_CERT_FILE")
CTX = ssl.create_default_context(cafile=CA) if CA else ssl.create_default_context()
SIZES = {}
# Upstream: the public download endpoint by default, or the authenticated
# Drive API when DRIVE_AUTH_HEADER ("Authorization: Bearer ...") is set.
# The API serves bounded ranged reads even when an unranged whole-file
# download is quota-blocked (OEV-006 probe run 39079662639).
AUTH = os.environ.get("DRIVE_AUTH_HEADER", "")


def url(fid):
    if AUTH:
        return f"https://www.googleapis.com/drive/v3/files/{fid}?alt=media"
    return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"


def headers(start, end):
    h = {"Range": f"bytes={start}-{end}", "User-Agent": "oev-range-proxy"}
    if AUTH:
        name, _, value = AUTH.partition(":")
        h[name.strip()] = value.strip()
    return h
def fetch(fid, start, end, attempts=6):
    """Ranged GET with bounded retries: Drive occasionally answers a ranged
    request with its HTML interstitial (no Content-Range) or a 5xx/429."""
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                url(fid),
                headers=headers(start, end),
            )
            r = urllib.request.urlopen(req, context=CTX, timeout=120)
            cr = r.headers.get("Content-Range")
            if r.status == 206 and cr:
                return r, int(cr.split("/")[1])
            r.close()
            last = f"status {r.status}, content-type {r.headers.get('Content-Type')}"
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # Quota/permission refusal: never hammer it.
                raise RuntimeError(f"range_proxy: {fid} bytes={start}-{end}: HTTP {e.code}, not retrying") from e
            last = repr(e)
        except Exception as e:  # noqa: BLE001 - logged and retried
            last = repr(e)
        sys.stderr.write(f"range_proxy: {fid} bytes={start}-{end} attempt {attempt + 1}: {last}\n")
        time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"range_proxy: giving up on {fid} bytes={start}-{end}: {last}")


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
        stalls = 0
        while pos <= end:
            piece_end = min(pos + CHUNK - 1, end)
            r, _ = fetch(fid, pos, piece_end)
            try:
                while pos <= piece_end:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    pos += len(chunk)
            except Exception as e:  # noqa: BLE001 - upstream dropped mid-piece
                sys.stderr.write(f"range_proxy: {fid} read error at {pos}: {e!r}\n")
            if pos <= piece_end:
                stalls += 1
                if stalls > 6:
                    return  # give up; the client sees a short read
                time.sleep(min(30, 2 ** stalls))
            else:
                stalls = 0


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    S(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
