#!/usr/bin/env python3
"""
Projector Frame Designer - a small local GUI in your web browser.

    python3 gui.py              # opens http://localhost:8765
    python3 gui.py --port 9000 --no-browser

Load a picture, drag it to choose which part sits behind the frame, scroll to
resize it, tune the light/dark threshold, then "Preview projection" (the real
stencil simulation) or "Generate STLs" (writes the parts to gui_out/ and
offers a .zip download). Everything runs locally; nothing is uploaded.
"""
import argparse
import base64
import io
import json
import os
import sys
import threading
import time
import traceback
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import projector_frame as pf  # noqa: E402

DEFAULT_IMAGE = os.path.join(HERE, "examples", "supra.jpg")
FRAME_KEYS = ("picture_w", "picture_h", "depth", "light_z", "max_magnification")
LOCK = threading.Lock()  # one heavy job at a time


def make_params(data):
    p = pf.Params()
    for k in FRAME_KEYS:
        if k in data.get("params", {}):
            setattr(p, k, float(data["params"][k]))
    if "threshold" in data:
        p.threshold = float(data["threshold"])
    p.fit = "stretch"  # the wall image is composed to exactly the lit area
    p.validate()
    return p


def decode_image(data_url):
    if not data_url:
        raise ValueError("No image loaded.")
    raw = base64.b64decode(data_url.split(",", 1)[-1])
    return Image.open(io.BytesIO(raw))


def png_data_url(path):
    with open(path, "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode()


def run_job(data, out_dir, preview_only):
    p = make_params(data)
    pic = decode_image(data.get("image"))
    wall = pf.compose_wall_image(pic, p, float(data["width"]),
                                 (float(data.get("cx", 0)), float(data.get("cy", 0))),
                                 invert=bool(data.get("invert")))
    log = []
    os.makedirs(out_dir, exist_ok=True)
    wall.save(os.path.join(out_dir, "wall_image.png"))
    with LOCK:
        files = pf.generate(p, wall, out_dir, preview_only, log=log.append)
    files.append(os.path.join(out_dir, "wall_image.png"))
    result = {"preview": png_data_url(os.path.join(out_dir, "preview.png")),
              "log": log, "warnings": [line for line in log if "WARNING" in line]}
    if not preview_only:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(f, os.path.basename(f))
        result["zip"] = "data:application/zip;base64," + base64.b64encode(buf.getvalue()).decode()
        result["files"] = [os.path.basename(f) for f in files]
        result["out_dir"] = os.path.relpath(out_dir, os.getcwd())
    return result


class Handler(BaseHTTPRequestHandler):
    out_root = "gui_out"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "gui.html"), "rb") as fh:
                return self.send(200, fh.read(), "text/html; charset=utf-8")
        if self.path == "/api/default-image" and os.path.exists(DEFAULT_IMAGE):
            with open(DEFAULT_IMAGE, "rb") as fh:
                return self.send(200, fh.read(), "image/jpeg")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/geometry":
                return self.send(200, pf.lit_area(make_params(data)))
            if self.path in ("/api/preview", "/api/build"):
                build = self.path == "/api/build"
                sub = time.strftime("%Y%m%d-%H%M%S") if build else "preview"
                out_dir = os.path.join(self.out_root, sub)
                print(("Building STLs in " if build else "Previewing in ") + out_dir, flush=True)
                return self.send(200, run_job(data, out_dir, preview_only=not build))
            self.send(404, {"error": "not found"})
        except SystemExit as e:  # Params.validate() reports problems this way
            self.send(400, {"error": str(e)})
        except Exception as e:  # report anything else to the page
            traceback.print_exc()
            self.send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out", default="gui_out", help="where generated files are saved")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    Handler.out_root = args.out
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"Projector Frame Designer running at {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
