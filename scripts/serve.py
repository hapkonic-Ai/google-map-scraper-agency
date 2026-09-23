#!/usr/bin/env python3
"""Local web UI for the Google Maps Scraper Kit (Python stdlib only, no pip installs).

Serves a single-page UI at http://127.0.0.1:8081 where you can:
  • run scrapes with point-and-click configuration (keyword, city/coords, depth, toggles)
  • watch the job status live, then auto-download + process the results
  • browse every saved results-*.csv in a filterable table (prospect badges included)

It proxies the scraper API (default http://localhost:8080) so the browser never
talks to it directly (avoids CORS), and reuses the kit's own logic:
scrape.geocode / scrape.enrich_socials / prospect.qualify.

Run:     python3 scripts/serve.py        (then open http://localhost:8081)
Stop:    Ctrl+C
"""
import csv, io, json, os, re, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib import request as urlreq
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape    # noqa: E402  (LEAD fields, geocode, enrich_socials)
import prospect  # noqa: E402  (qualify, PROSPECT_FIELDS)

ROOT = Path(__file__).resolve().parent.parent          # kit root (where results-*.csv live)
UI_FILE = Path(__file__).resolve().parent / "ui.html"
BASE = os.environ.get("SCRAPER_BASE_URL", "http://localhost:8080")
HOST = "127.0.0.1"                                      # localhost only, same rule as the scraper
PORT = int(os.environ.get("UI_PORT", "8081"))
MAX_ROWS = 500                                          # rows sent to the browser per results view


def proxy_api(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urlreq.Request(BASE + path, data=data, method=method,
                       headers={"Content-Type": "application/json"})
    try:
        with urlreq.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urlreq.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 502, json.dumps({"error": f"scraper not reachable at {BASE} — run 'docker compose up -d' ({e})"}).encode()


def safe_csv(name):
    """Resolve a results file name to a real file inside ROOT, or None."""
    name = unquote(name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.csv", name):
        return None
    p = (ROOT / name).resolve()
    return p if p.parent == ROOT.resolve() and p.is_file() else None


def list_results():
    out = []
    for p in sorted(ROOT.glob("results-*.csv"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            with open(p, newline="", encoding="utf-8-sig") as f:
                rows = max(sum(1 for _ in f) - 1, 0)
        except Exception:
            rows = -1
        st = p.stat()
        out.append({"name": p.name, "rows": rows, "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


def result_detail(p):
    with open(p, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    fields = list(rows[0].keys()) if rows else []
    return {"fields": fields, "total": len(rows), "rows": rows[:MAX_ROWS]}


def finalize(job_id, do_prospects, do_socials):
    """Download a finished job, trim to lead fields, optionally add socials/prospect
    columns, save as results-<id>.csv in the kit root. Returns a summary dict."""
    if not re.fullmatch(r"[a-f0-9\-]{36}", job_id or ""):
        return {"error": "bad job id"}
    code, raw = proxy_api("GET", f"/api/v1/jobs/{job_id}/download", timeout=120)
    if code != 200:
        return {"error": f"download failed: HTTP {code}"}
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
    fields = list(scrape.LEAD)
    results = [{k: r.get(k, "") for k in fields} for r in rows]
    if do_socials and results:
        print(f"[ui] fetching socials for {len(results)} sites…")
        scrape.enrich_socials(results)
        fields += ["instagram", "facebook", "linkedin"]
    if do_prospects:
        prospect.qualify(results)  # sorts best-first, adds prospect columns
        fields += [c for c in prospect.PROSPECT_FIELDS if c not in fields]
    fname = f"results-{job_id[:8]}.csv"
    with open(ROOT / fname, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)
    print(f"[ui] saved {len(results)} rows -> {fname}")
    return {"file": fname, "total": len(results),
            "needs_website": sum(1 for r in results if r.get("needs_website") == "yes"),
            "hot": sum(1 for r in results if r.get("priority") == "hot")}


class UI(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet console
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path in ("/", "/index.html"):
            return self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/health":
            code, raw = proxy_api("GET", "/api/v1/jobs")
            n = 0
            if code == 200:
                try:
                    n = len(json.loads(raw))
                except Exception:
                    pass
            return self._send(200, {"scraper": code == 200, "base": BASE, "jobs": n})

        if path == "/api/geocode":
            place = q.get("q", [""])[0].strip()
            if not place:
                return self._send(400, {"error": "missing ?q="})
            coords = scrape.geocode(place)  # Nominatim; sleeps 1s to respect rate limit
            return self._send(200, {"lat": coords[0], "lon": coords[1]} if coords
                              else {"error": f"could not geocode '{place}'"})

        if path == "/api/jobs":
            code, raw = proxy_api("GET", "/api/v1/jobs")
            if code != 200:
                return self._send(200, {"scraper_down": True, "detail": raw.decode("utf-8", "replace")})
            return self._send(200, raw)

        m = re.fullmatch(r"/api/jobs/([a-f0-9\-]{36})", path)
        if m:
            code, raw = proxy_api("GET", f"/api/v1/jobs/{m.group(1)}")
            return self._send(code, raw)

        if path == "/api/results":
            name = q.get("name", [""])[0]
            if not name:
                return self._send(200, list_results())
            p = safe_csv(name)
            if not p:
                return self._send(404, {"error": "file not found"})
            if q.get("download"):
                return self._send(200, p.read_bytes(), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{p.name}"'})
            return self._send(200, result_detail(p))

        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad JSON body"})

        if path == "/api/jobs":
            keywords = [k.strip() for k in body.get("keywords", []) if k.strip()]
            if not keywords:
                return self._send(400, {"error": "no keywords"})
            lat, lon = body.get("lat"), body.get("lon")
            if not (lat and lon):
                return self._send(400, {"error": "missing coordinates — geocode the city first"})
            job = {"name": body.get("name") or "ui-scrape", "keywords": keywords, "lang": "en",
                   "zoom": 15, "lat": str(lat), "lon": str(lon), "fast_mode": False,
                   "radius": int(body.get("radius") or 10000),
                   "depth": int(body.get("depth") or 5),
                   "email": bool(body.get("email", True)),
                   "max_time": int(body.get("max_time") or 600)}
            code, raw = proxy_api("POST", "/api/v1/jobs", job)
            return self._send(code, raw)

        if path == "/api/finalize":
            return self._send(200, finalize(body.get("job_id", ""),
                                            bool(body.get("prospects")),
                                            bool(body.get("socials"))))

        self._send(404, {"error": "not found"})


def main():
    if not UI_FILE.is_file():
        sys.exit(f"ui.html not found next to serve.py ({UI_FILE})")
    code, _ = proxy_api("GET", "/api/v1/jobs", timeout=5)
    state = "up" if code == 200 else f"DOWN ({BASE}) — start it with 'docker compose up -d'"
    print(f"Google Maps Scraper UI → http://{HOST}:{PORT}")
    print(f"Scraper API {BASE}: {state}")
    print("Ctrl+C to stop.")
    try:
        ThreadingHTTPServer((HOST, PORT), UI).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
