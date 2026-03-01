"""Thin SSE server wiring dev.html → sigma_combined.py subprocess.

Serves all HTML files (landing, app, dev) and the /run SSE endpoint.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

ROOT = Path(__file__).parent
SCRIPT = ROOT / "v2" / "sigma_combined.py"


@app.get("/")
async def index():
    return FileResponse(ROOT / "landing.html")


@app.get("/dev")
@app.get("/dev.html")
async def dev():
    return FileResponse(ROOT / "dev.html")


@app.get("/app")
@app.get("/app.html")
async def swiper():
    return FileResponse(ROOT / "app.html")


@app.get("/landing")
@app.get("/landing.html")
async def landing():
    return FileResponse(ROOT / "landing.html")


@app.get("/run")
async def run(url: str = Query(...)):
    async def stream():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPT), url,
            "--signup-timeout", "90",
            "--login-timeout", "90",
            "--verify-timeout", "30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(ROOT),
        )
        result = {}
        in_results = False
        results_lines = []

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")

            # Detect results block
            if line.strip().startswith("=" * 10):
                in_results = not in_results
                results_lines.append(line)
                continue

            if in_results:
                results_lines.append(line)
                # Parse "  Key:    value" lines
                m = re.match(r"\s+(\w[\w ]*?):\s+(.+)", line)
                if m:
                    key = m.group(1).strip().lower().replace(" ", "_")
                    val = m.group(2).strip()
                    result[key] = val
                continue

            # Only stream sigma log lines (timestamped) — skip browser-use noise
            if re.match(r"\s*\[\s*[\d.]+s\]", line):
                yield f"event: log\ndata: {json.dumps(line)}\n\n"

        await proc.wait()

        # Send parsed results
        if result:
            yield f"event: result\ndata: {json.dumps(result)}\n\n"

        yield f"event: done\ndata: {json.dumps({'code': proc.returncode})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
