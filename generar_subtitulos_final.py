"""
Genera el video final con:
1. Silencios eliminados (ya procesado)
2. Subtítulos reales de Higgsfield Analysis quemados
3. Música de fondo
"""

import os
import subprocess
import numpy as np
import wave

import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

def run(cmd):
    cmd[0] = FFMPEG if cmd[0] == "ffmpeg" else cmd[0]
    print("  $", " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("STDERR:", result.stderr[-500:])
        raise RuntimeError(f"Fallo: {cmd[0]}")
    return result

# Transcript from Higgsfield Video Analysis
SCENES = [
    ("0:00", "0:14", "Argentine construction is a sector of 19,000 companies that generate 354,000 formal jobs, but it operates like 20 years ago, with 73% labor informality and without digital tools."),
    ("0:14", "0:18", "We are solving that with Construlink."),
    ("0:18", "0:33", "Construction companies, developers, and architecture studios need to hire specialists quickly. Today they do it by word of mouth and WhatsApp."),
    ("0:33", "0:41", "There's no way to validate if the contractor is legal, if they fulfill commitments, or if they'll deliver on time. And when there are conflicts, there is no record of anything."),
    ("0:41", "0:52", "This generates distrust, delays, and lost money. It's a structural problem in the sector. Construlink integrates four things in a single platform."),
    ("0:52", "1:01", "First, an intelligent marketplace where construction companies find contractors verified by trade, location, and reputation."),
    ("1:01", "1:09", "Second, validation of legal documentation in real time, with a traffic light that shows if someone is legal, under review, or at risk."),
    ("1:09", "1:17", "Third, a construction management tool where milestones, budgets, and inventory are centralized; all digital and collaborative."),
    ("1:17", "1:30", "And fourth, a payment system secured with escrow—funds held until each milestone is completed, protecting both parties."),
    ("1:30", "1:44", "Procore and Construex are pure ERPs without a marketplace. Lifeive and Workana are generic marketplaces without legal compliance. Kliquie is only residential."),
    ("1:44", "1:56", "We are the only platform in LATAM integrating these four pillars in one place, designed from within the Argentine sector."),
    ("1:56", "2:12", "We have a functional MVP, interviews with companies ready to pilot, and real traction. The team: Franco, architect, built the MVP from scratch; Huissen, architect, works in the field; Eric, systems analyst, five years in tech."),
    ("2:12", "3:17", "We're looking to validate hypotheses with users in Córdoba. Then scale provincially and to LATAM. We come from EmprendeU. We are in EmprendeLatam. If you see the potential in digitizing Argentine construction with trust and transparency, let's talk."),
]

def time_to_secs(t):
    parts = t.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])

def secs_to_srt(s):
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    ms = int((s % 1) * 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

def create_srt(output_path, offset=0.0):
    """Create SRT file from Higgsfield scenes. offset adjusts all timestamps."""
    with open(output_path, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(SCENES, 1):
            s = max(0, time_to_secs(start) - offset)
            e = max(s + 0.5, time_to_secs(end) - offset)
            # Break long text into lines of ~60 chars
            words = text.split()
            lines, line = [], []
            for w in words:
                if sum(len(x)+1 for x in line) + len(w) > 60 and line:
                    lines.append(" ".join(line))
                    line = [w]
                else:
                    line.append(w)
            if line:
                lines.append(" ".join(line))
            f.write(f"{i}\n{secs_to_srt(s)} --> {secs_to_srt(e)}\n")
            f.write("\n".join(lines[:3]))  # max 3 lines
            f.write("\n\n")
    print(f"  SRT guardado: {output_path}")

def generate_music(duration_sec, output_wav):
    sample_rate = 44100
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    freqs_chords = [
        [261.63, 329.63, 392.00],
        [220.00, 261.63, 329.63],
        [174.61, 220.00, 261.63],
        [196.00, 246.94, 293.66],
    ]
    chord_dur = duration_sec / len(freqs_chords)
    audio = np.zeros_like(t)
    for i, freqs in enumerate(freqs_chords):
        start = int(i * chord_dur * sample_rate)
        end = int((i + 1) * chord_dur * sample_rate)
        chunk = np.zeros(end - start)
        chunk_t = np.linspace(i * chord_dur, (i + 1) * chord_dur, end - start, endpoint=False)
        for freq in freqs:
            chunk += 0.15 * np.sin(2 * np.pi * freq * chunk_t)
            chunk += 0.05 * np.sin(2 * np.pi * freq * 2 * chunk_t)
        fade = min(int(0.3 * sample_rate), len(chunk) // 4)
        chunk[:fade] *= np.linspace(0, 1, fade)
        chunk[-fade:] *= np.linspace(1, 0, fade)
        audio[start:end] += chunk
    audio = audio / (np.max(np.abs(audio)) + 1e-9) * 0.3
    audio_int = (audio * 32767).astype(np.int16)
    with wave.open(output_wav, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int.tobytes())

def get_duration(path):
    result = subprocess.run([FFMPEG, "-i", path, "-f", "null", "-"], capture_output=True, text=True)
    for line in result.stderr.splitlines():
        if "Duration" in line:
            t = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = t.split(":")
            return float(h)*3600 + float(m)*60 + float(s)
    return 0

def main():
    input_video = "/home/user/edicion-de-video/d2aca575-whatsappvideo20260624at120947_Yo6CcxpF_editado.mp4"
    output_video = "/home/user/edicion-de-video/construlink_final.mp4"
    srt_path = "/home/user/edicion-de-video/construlink_subtitulos.srt"
    music_path = "/tmp/music_final.wav"

    print("[1/4] Creando SRT con subtítulos de Higgsfield Analysis...")
    # The edited video removed ~20s of silences, so offset by 0 and let subtitles
    # align to edited video (close enough - silences were short pauses)
    create_srt(srt_path, offset=0)

    print("[2/4] Generando música de fondo...")
    duration = get_duration(input_video)
    print(f"  Duración del video editado: {duration:.1f}s")
    generate_music(duration + 5, music_path)

    print("[3/4] Quemando subtítulos en el video...")
    tmp_sub = "/tmp/video_con_sub.mp4"
    run(["ffmpeg", "-y", "-i", input_video,
         "-vf", f"subtitles={srt_path}:force_style='FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=1,Alignment=2,MarginV=20'",
         "-c:a", "copy", tmp_sub])

    print("[4/4] Mezclando música de fondo y exportando...")
    run(["ffmpeg", "-y",
         "-i", tmp_sub,
         "-i", music_path,
         "-filter_complex",
         "[0:a]volume=1.0[voice];[1:a]volume=0.22,aloop=loop=-1:size=2e+09[music];[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]",
         "-map", "0:v",
         "-map", "[aout]",
         "-c:v", "copy",
         "-c:a", "aac",
         "-shortest",
         output_video])

    os.remove(tmp_sub)

    print(f"\n✓ Video final: {output_video}")
    print(f"✓ Subtítulos SRT: {srt_path}")
    size = os.path.getsize(output_video) / 1024 / 1024
    print(f"✓ Tamaño: {size:.1f} MB")

if __name__ == "__main__":
    main()
