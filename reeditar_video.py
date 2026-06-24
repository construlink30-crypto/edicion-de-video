"""
Re-edición limpia:
- Sin subtítulos
- Sin música
- Corta los retakes repetidos (2:12-2:57) detectados por Higgsfield
- Elimina silencios con umbral conservador para evitar cortes bruscos
"""

import os
import subprocess
import tempfile
import shutil

import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

INPUT = "/root/.claude/uploads/a714719c-737b-5e88-833a-3941880c953f/d2aca575-whatsappvideo20260624at120947_Yo6CcxpF.mp4"
OUTPUT = "/home/user/edicion-de-video/construlink_limpio.mp4"

def run(cmd, check=True):
    cmd[0] = FFMPEG if cmd[0] == "ffmpeg" else cmd[0]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print("STDERR:", result.stderr[-600:])
        raise RuntimeError(f"Fallo: {cmd}")
    return result

def get_duration(path):
    result = run([FFMPEG, "-i", path, "-f", "null", "-"], check=False)
    for line in result.stderr.splitlines():
        if "Duration" in line:
            h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
            return float(h)*3600 + float(m)*60 + float(s)
    return 0

def detect_silences(wav, thresh_db=-30, min_dur=0.5):
    result = subprocess.run(
        [FFMPEG, "-i", wav,
         "-af", f"silencedetect=noise={thresh_db}dB:d={min_dur}",
         "-f", "null", "-"],
        capture_output=True, text=True
    )
    silences, start = [], None
    for line in result.stderr.splitlines():
        if "silence_start" in line:
            start = float(line.split("silence_start:")[1].strip().split()[0])
        elif "silence_end" in line and start is not None:
            end = float(line.split("silence_end:")[1].strip().split("|")[0].strip())
            silences.append((start, end))
            start = None
    return silences

def extract_wav(video, wav):
    run([FFMPEG, "-y", "-i", video, "-ac", "1", "-ar", "16000", "-vn", "-f", "wav", wav])

def cut_segment(src, start, end, dst):
    run([FFMPEG, "-y", "-ss", str(start), "-to", str(end), "-i", src,
         "-c", "copy", "-avoid_negative_ts", "make_zero", dst])

def concat(parts, dst):
    tmp = tempfile.mktemp(suffix=".txt")
    with open(tmp, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", tmp,
         "-c", "copy", dst])
    os.remove(tmp)

def build_keep_ranges(duration, silences, padding=0.08):
    """Invert silence list into speech ranges."""
    ranges, cursor = [], 0.0
    for s, e in silences:
        if s - padding > cursor:
            ranges.append((cursor, s + padding))
        cursor = max(cursor, e - padding)
    if cursor < duration - 0.1:
        ranges.append((cursor, duration))
    return ranges

def main():
    tmp = tempfile.mkdtemp(prefix="recut_")
    try:
        total = get_duration(INPUT)
        print(f"Duración original: {total:.1f}s ({total/60:.1f} min)")

        # ── Step 1: define macro segments (cut repeated takes 2:12–2:57)
        # Higgsfield confirmed scenes 13-15 are the same closing repeated with errors
        # Keep: 0:00–2:12, then jump to 2:57–end
        MACRO = [
            (0.0,   132.0),   # 0:00 – 2:12  (good content)
            (177.0, total),   # 2:57 – end   (final clean take)
        ]
        print(f"Macro cortes: eliminando retakes 2:12–2:57 ({177-132:.0f}s de repeticiones)")

        # ── Step 2: extract each macro segment, detect+remove silences within it
        all_parts = []
        part_idx = 0

        for m_start, m_end in MACRO:
            seg_video = os.path.join(tmp, f"macro_{part_idx}.mp4")
            cut_segment(INPUT, m_start, m_end, seg_video)

            seg_wav = os.path.join(tmp, f"macro_{part_idx}.wav")
            extract_wav(seg_video, seg_wav)

            seg_dur = get_duration(seg_video)
            silences = detect_silences(seg_wav, thresh_db=-30, min_dur=0.5)
            print(f"  Segmento [{m_start:.0f}s–{m_end:.0f}s]: {len(silences)} silencios detectados")

            speech_ranges = build_keep_ranges(seg_dur, silences)

            for r_start, r_end in speech_ranges:
                if r_end - r_start < 0.2:
                    continue
                part = os.path.join(tmp, f"part_{part_idx:04d}.mp4")
                cut_segment(seg_video, r_start, r_end, part)
                all_parts.append(part)
                part_idx += 1

        print(f"\nConcatenando {len(all_parts)} fragmentos...")
        concat(all_parts, OUTPUT)

        final_dur = get_duration(OUTPUT)
        print(f"\n✓ Video listo: {OUTPUT}")
        print(f"✓ Duración final: {final_dur:.1f}s ({final_dur/60:.1f} min)")
        print(f"✓ Reducción: {total - final_dur:.1f}s eliminados")
        print(f"✓ Tamaño: {os.path.getsize(OUTPUT)/1024/1024:.1f} MB")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()
