import os
import re
import subprocess
import uuid
from pathlib import Path

import imageio_ffmpeg
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI()
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")


def parse_prompt(prompt: str, duration: float) -> list[str]:
    """Convert natural language prompt to ffmpeg filter args."""
    prompt_lower = prompt.lower()
    filters = []
    extra_args = []

    # --- trim / cut ---
    trim_match = re.search(
        r"recorta?\s+(?:del?\s+)?(?:segundo\s+)?(\d+(?:\.\d+)?)\s+(?:al?|hasta)\s+(?:el?\s+)?(?:segundo\s+)?(\d+(?:\.\d+)?)",
        prompt_lower,
    )
    if not trim_match:
        trim_match = re.search(
            r"corta?\s+(?:del?\s+)?(\d+(?:\.\d+)?)\s+(?:al?|hasta)\s+(\d+(?:\.\d+)?)",
            prompt_lower,
        )
    start, end = 0.0, duration
    if trim_match:
        start = float(trim_match.group(1))
        end = float(trim_match.group(2))
        extra_args += ["-ss", str(start), "-to", str(end)]

    # --- speed ---
    speed_match = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(?:de\s+)?velocidad|velocidad\s+(\d+(?:\.\d+)?)", prompt_lower)
    if not speed_match:
        speed_match = re.search(r"(doble|triple|mitad)\s*(?:de\s+)?velocidad", prompt_lower)
    speed = 1.0
    if speed_match:
        word = speed_match.group(0)
        if "doble" in word:
            speed = 2.0
        elif "triple" in word:
            speed = 3.0
        elif "mitad" in word:
            speed = 0.5
        else:
            val = speed_match.group(1) or speed_match.group(2)
            if val:
                speed = float(val)
    if speed != 1.0:
        filters.append(f"setpts={1/speed}*PTS")

    # --- text overlay ---
    text_match = re.search(
        r'(?:agrega?|pon|escribe|a[ñn]ade?)\s+(?:el\s+)?(?:texto\s+)?["\']?([^"\']+?)["\']?\s*(?:arriba|abajo|encima|centro|en\s+|$)',
        prompt_lower,
    )
    position = "bottom"
    if "arriba" in prompt_lower or "encima" in prompt_lower:
        position = "top"
    elif "centro" in prompt_lower or "center" in prompt_lower:
        position = "center"

    if text_match:
        text = text_match.group(1).strip().rstrip("'\"")
        y_pos = {"top": "50", "center": "(h-text_h)/2", "bottom": "h-th-50"}[position]
        filters.append(
            f"drawtext=text='{text}':fontcolor=white:fontsize=48:borderw=3:bordercolor=black"
            f":x=(w-text_w)/2:y={y_pos}"
        )

    # --- resolution ---
    if "720p" in prompt_lower:
        filters.append("scale=1280:720")
    elif "1080p" in prompt_lower:
        filters.append("scale=1920:1080")
    elif "480p" in prompt_lower:
        filters.append("scale=854:480")

    # --- rotate ---
    if "rotar 90" in prompt_lower or "girar 90" in prompt_lower:
        filters.append("transpose=1")
    elif "rotar 180" in prompt_lower or "girar 180" in prompt_lower:
        filters.append("transpose=1,transpose=1")
    elif "rotar 270" in prompt_lower or "girar 270" in prompt_lower:
        filters.append("transpose=2")

    # --- mute audio ---
    if "quita" in prompt_lower and "audio" in prompt_lower or "sin audio" in prompt_lower or "sin sonido" in prompt_lower:
        extra_args += ["-an"]

    return filters, extra_args


def get_duration(path: str) -> float:
    result = subprocess.run(
        [FFMPEG, "-i", path],
        capture_output=True, text=True
    )
    m = re.search(r"Duration:\s+(\d+):(\d+):(\d+\.?\d*)", result.stderr)
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 0.0


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(open("index.html", encoding="utf-8").read())


@app.post("/editar")
async def editar(
    video: UploadFile = File(...),
    prompt: str = Form(...),
):
    ext = Path(video.filename).suffix or ".mp4"
    uid = uuid.uuid4().hex
    input_path = UPLOAD_DIR / f"{uid}_input{ext}"
    output_path = OUTPUT_DIR / f"{uid}_output.mp4"

    with open(input_path, "wb") as f:
        f.write(await video.read())

    duration = get_duration(str(input_path))
    filters, extra_args = parse_prompt(prompt, duration)

    cmd = [FFMPEG, "-y"]
    cmd += extra_args
    cmd += ["-i", str(input_path)]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-c:a", "aac", str(output_path)]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0 or not output_path.exists():
        return {"error": result.stderr[-1000:]}

    return {"url": f"/outputs/{output_path.name}"}


@app.get("/descargar/{filename}")
async def descargar(filename: str):
    path = OUTPUT_DIR / filename
    if not path.exists():
        return {"error": "Archivo no encontrado"}
    return FileResponse(path, media_type="video/mp4", filename=filename)
