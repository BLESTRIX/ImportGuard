"""Refresh locally served, pinned upstream assets. No runtime CDN dependency."""

import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

ASSETS = {
    "swagger-ui.css": "https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.1/dist/swagger-ui.css",
    "swagger-ui-bundle.js": "https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.1/dist/swagger-ui-bundle.js",
    "swagger-LICENSE": "https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.1/LICENSE",
    "swagger-NOTICE": "https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.1/NOTICE",
    "bootstrap.min.css": "https://raw.githubusercontent.com/twbs/bootstrap/v5.3.8/dist/css/bootstrap.min.css",
    "bootstrap-LICENSE": "https://raw.githubusercontent.com/twbs/bootstrap/v5.3.8/LICENSE",
    "htmx.min.js": "https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.8/dist/htmx.min.js",
    "htmx-LICENSE": "https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.8/LICENSE",
}


def main():
    root = Path(__file__).parents[1] / "app/static/vendor"
    root.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, url in ASSETS.items():
        with urlopen(url, timeout=30) as response:
            content = response.read()
        (root / name).write_bytes(content)
        manifest[name] = {"url": url, "sha256": hashlib.sha256(content).hexdigest()}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        "Vendored Bootstrap 5.3.8, HTMX 2.0.8, and Swagger UI 5.33.1 with licenses and checksums."
    )


if __name__ == "__main__":
    main()
