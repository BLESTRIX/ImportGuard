"""Real HTTP + PostgreSQL + MinIO + Celery recovery demonstration (development only)."""

import argparse
import csv
import io
import json
import subprocess
import time
import uuid
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


class API:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.cookies = CookieJar()
        self.http = build_opener(HTTPCookieProcessor(self.cookies))
        self.request("GET", "/login", raw=True)

    def request(self, method, path, data=None, headers=None, raw=False):
        headers = headers or {}
        if method not in ("GET", "HEAD"):
            headers["X-CSRF-Token"] = next(c.value for c in self.cookies if c.name == "ig_csrf")
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.http.open(request, timeout=30) as response:
                result = response.read()
        except HTTPError as exc:
            raise RuntimeError(f"{method} {path}: {exc.code} {exc.read().decode()}") from exc
        return result if raw else json.loads(result)

    def upload(self, name, content):
        boundary = uuid.uuid4().hex
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\n{name}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="supplier.csv"\r\n'
                "Content-Type: text/csv\r\n\r\n"
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return self.request(
            "POST",
            "/api/v1/imports",
            body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def wait(self, import_id, statuses, timeout=240):
        deadline = time.monotonic() + timeout
        previous = None
        while time.monotonic() < deadline:
            item = self.request("GET", f"/api/v1/imports/{import_id}")
            progress = (item["status"], item["checkpoint"])
            if progress != previous:
                print(
                    f"  {progress[0]} · checkpoint {progress[1]} · valid {item['valid_count']} / invalid {item['invalid_count']}",
                    flush=True,
                )
                previous = progress
            if item["status"] in statuses:
                return item
            if item["status"] in ("FAILED", "CANCELED", "STALE"):
                raise RuntimeError(item["failure_message"] or item["status"])
            time.sleep(1)
        raise TimeoutError(f"Timed out waiting for {statuses}")


def docker(*args, expect=0):
    result = subprocess.run(
        ["docker", "compose", *args], cwd=Path(__file__).parents[1], check=False
    )
    if result.returncode != expect:
        raise RuntimeError(f"Docker command returned {result.returncode}; expected {expect}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    api = API(args.base_url)
    suffix = uuid.uuid4().hex[:10]
    email, password = f"demo-{suffix}@example.com", "ImportGuard-demo-2026!"
    api.request(
        "POST",
        "/api/v1/auth/register",
        {
            "email": email,
            "password": password,
            "organization_name": f"Recovery demo {suffix}",
            "currency": "USD",
        },
    )
    mapping = {
        "sku": "product_code",
        "name": "product_name",
        "unit_price": "price",
        "stock_quantity": "qty",
        "category": "category",
    }
    print("1. Upload an invalid source and inspect its report", flush=True)
    invalid = api.upload(
        "Invalid supplier delivery",
        (Path(__file__).parents[1] / "sample_data/invalid_inventory.csv").read_bytes(),
    )
    api.request("PUT", f"/api/v1/imports/{invalid['id']}/mapping", {"mapping": mapping})
    api.request("POST", f"/api/v1/imports/{invalid['id']}/validate")
    api.wait(invalid["id"], {"INVALID"})
    report = api.request("GET", f"/api/v1/imports/{invalid['id']}/errors.csv", raw=True).decode()
    print(report, flush=True)
    print("2. Upload a corrected 1,200-record file", flush=True)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(mapping.values())
    for index in range(1200):
        writer.writerow([f"RECOVERY-{index:04}", f"Product {index}", "4.50", index % 50, "Demo"])
    item = api.upload("Corrected recovery delivery", buffer.getvalue().encode())
    api.request("PUT", f"/api/v1/imports/{item['id']}/mapping", {"mapping": mapping})
    print("3. Stop the worker; arm and run a crash after one committed batch", flush=True)
    docker("stop", "worker")
    try:
        docker(
            "exec",
            "-T",
            "-e",
            "ENABLE_FAILURE_INJECTION=true",
            "web",
            "python",
            "-m",
            "scripts.manage",
            "failpoint",
            item["id"],
            "crash_after_batch",
        )
        job = api.request("POST", f"/api/v1/imports/{item['id']}/validate")
        docker(
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "ENABLE_FAILURE_INJECTION=true",
            "-e",
            "LEASE_SECONDS=5",
            "worker",
            "python",
            "-m",
            "scripts.manage",
            "work-once",
            "--job-id",
            job["job_id"],
            expect=75,
        )
        interrupted = api.request("GET", f"/api/v1/imports/{item['id']}")
        assert 0 < interrupted["checkpoint"] < 1200, interrupted
        assert api.request("GET", "/api/v1/products")["total"] == 0
        print(
            f"  Confirmed durable checkpoint: {interrupted['checkpoint']}; inventory remains empty",
            flush=True,
        )
    finally:
        docker("start", "worker")
    print("4. Restarted worker resumes after the expired lease", flush=True)
    ready = api.wait(item["id"], {"READY"})
    assert ready["processed"] == 1200 and ready["counts"]["create"] == 1200
    print("5. Review the preview, approve it, and repeat the apply request", flush=True)
    preview = api.request("GET", f"/api/v1/imports/{item['id']}/preview")
    print(f"  Preview counts: {preview['counts']}", flush=True)
    body = {"confirmed": True, "preview_revision": ready["preview_revision"]}
    headers = {"Idempotency-Key": f"recovery-{suffix}"}
    first = api.request("POST", f"/api/v1/imports/{item['id']}/apply", body, dict(headers))
    second = api.request("POST", f"/api/v1/imports/{item['id']}/apply", body, dict(headers))
    assert first["job_id"] == second["job_id"]
    api.wait(item["id"], {"APPLIED"})
    replay = api.request("POST", f"/api/v1/imports/{item['id']}/apply", body, dict(headers))
    assert replay["job_id"] == first["job_id"]
    products = api.request("GET", "/api/v1/products?page_size=100")
    assert products["total"] == 1200 and all(p["version"] == 1 for p in products["items"])
    events = []
    number = 1
    while True:
        page = api.request("GET", f"/api/v1/audit-events?page_number={number}&page_size=100")
        events.extend(page["items"])
        if len(events) >= page["total"]:
            break
        number += 1
    changes = [e for e in events if e["event"] == "import.product_create"]
    assert len(changes) == 1200 and all(
        e["previous"] is None and e["new"]["version"] == 1 for e in changes
    )
    print(
        f"6. Verified 1,200 products, 1,200 complete change events, and one apply job.\nOpen {args.base_url}/imports/{item['id']}\nDemo login: {email}\nPassword: {password}",
        flush=True,
    )


if __name__ == "__main__":
    main()
