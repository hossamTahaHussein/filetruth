"""Build a single HTML file containing only tracked synthetic examples and the built viewer."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
dist = ROOT / "web/dist"
html = (dist / "index.html").read_text()
scripts = re.findall(r'<script[^>]+src="([^"]+)"[^>]*></script>', html)
styles = re.findall(r'<link[^>]+href="([^"]+\.css)"[^>]*>', html)
assert len(scripts) == len(styles) == 1, (
    "Build the production frontend before packaging."
)
js = (dist / scripts[0].lstrip("/")).read_text()
css = (dist / styles[0].lstrip("/")).read_text()
html = re.sub(r'<script[^>]+src="[^"]+"[^>]*></script>', "", html)
html = re.sub(
    r'<link[^>]+href="[^"]+\.css"[^>]*>', lambda _: "<style>" + css + "</style>", html
)
samples = {
    path.stem: json.loads(path.read_text())
    for path in sorted((ROOT / "examples").glob("*.json"))
    if path.name != "benchmark.json"
}
payload = json.dumps(samples, ensure_ascii=False).replace("<", "\\u003c")
html = html.replace(
    "</body>",
    '<script type="application/json" id="filetruth-examples">'
    + payload
    + '</script><script type="module">'
    + js.replace("</script", "<\\/script")
    + "</script></body>",
)
notices = (
    (ROOT / "web/public/third-party.txt").read_text().replace("</script", "<\\/script")
)
html = html.replace(
    "</body>",
    '<script type="text/plain" id="third-party-notices">'
    + notices
    + "</script></body>",
)
(ROOT / "examples/demo.html").write_text(re.sub(r"(?m)^[ \t]+$", "", html))
