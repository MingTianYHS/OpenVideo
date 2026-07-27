"""Regression tests for audio_mixer full_mix target_duration (issue #361).

full_mix builds its final stage with amix=...:duration=longest, but the
ducked-music branch is gated behind the speech sidechain, so "longest" tracks
the speech bus and the music tail past the last narration is dropped. A caller
that knows its video length had no way to state it, so the mixed audio ended
early (a 6s music bed under 2s of narration produced a 2s mix). target_duration
pads with silence and hard-trims so the output is exactly the composition
length, while an unset target keeps the historical behavior.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.audio.audio_mixer import AudioMixer  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe required for full_mix duration tests",
)


def _sine(path: Path, freq: int, dur: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}", str(path)],
        capture_output=True,
        check=True,
        timeout=30,
    )


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return float(out.stdout.strip())


def _tracks(tmp_path: Path):
    speech, music = tmp_path / "speech.wav", tmp_path / "music.wav"
    _sine(speech, 440, 2)  # narration ends at 2s
    _sine(music, 220, 6)   # music bed runs to 6s
    return [
        {"path": str(speech), "role": "speech"},
        {"path": str(music), "role": "music"},
    ]


def test_target_duration_preserves_music_tail_past_speech(tmp_path):
    # Without target_duration the ducking graph truncates to the 2s speech bus.
    # Stating the 6s composition length must keep the full music tail.
    out = tmp_path / "mixed.wav"
    result = AudioMixer().execute(
        {
            "operation": "full_mix",
            "tracks": _tracks(tmp_path),
            "ducking": {"enabled": True},
            "target_duration": 6,
            "output_path": str(out),
        }
    )
    assert result.success is True, result.error
    assert result.data["target_duration"] == 6
    assert _duration(out) == pytest.approx(6.0, abs=0.15)


def test_target_duration_pads_when_longer_than_all_tracks(tmp_path):
    # Target beyond every input must pad with silence, not clamp to the longest.
    out = tmp_path / "padded.wav"
    result = AudioMixer().execute(
        {
            "operation": "full_mix",
            "tracks": _tracks(tmp_path),
            "ducking": {"enabled": True},
            "target_duration": 9,
            "output_path": str(out),
        }
    )
    assert result.success is True, result.error
    assert _duration(out) == pytest.approx(9.0, abs=0.15)


def test_unset_target_duration_keeps_legacy_length(tmp_path):
    # Backward compatibility: with no target the output still follows the
    # surviving bus (2s here), and the reported target_duration is None.
    out = tmp_path / "legacy.wav"
    result = AudioMixer().execute(
        {
            "operation": "full_mix",
            "tracks": _tracks(tmp_path),
            "ducking": {"enabled": True},
            "output_path": str(out),
        }
    )
    assert result.success is True, result.error
    assert result.data["target_duration"] is None
    assert _duration(out) == pytest.approx(2.0, abs=0.15)


def test_target_duration_without_ducking(tmp_path):
    # The non-ducking amix path must honor target_duration too.
    out = tmp_path / "noduck.wav"
    result = AudioMixer().execute(
        {
            "operation": "full_mix",
            "tracks": _tracks(tmp_path),
            "ducking": {"enabled": False},
            "target_duration": 6,
            "output_path": str(out),
        }
    )
    assert result.success is True, result.error
    assert _duration(out) == pytest.approx(6.0, abs=0.15)
