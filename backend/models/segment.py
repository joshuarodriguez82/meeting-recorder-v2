"""Transcript segment model — a single spoken utterance."""

from dataclasses import dataclass


@dataclass
class Segment:
    """A time-bounded spoken segment attributed to one speaker."""

    speaker_id: str
    start: float        # seconds
    end: float          # seconds
    text: str

    def to_dict(self) -> dict:
        return {
            "speaker_id": self.speaker_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }

    def formatted(self, display_name: str) -> str:
        """Human-readable line for display and export."""
        start_str = self._format_time(self.start)
        end_str = self._format_time(self.end)
        return f"[{start_str} → {end_str}] {display_name}: {self.text}"

    @staticmethod
    def _format_time(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}"