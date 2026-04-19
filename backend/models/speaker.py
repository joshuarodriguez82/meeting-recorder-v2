"""Speaker identity model."""

from dataclasses import dataclass, field
import uuid


@dataclass
class Speaker:
    """Represents a detected speaker in a meeting session."""

    speaker_id: str = field(default_factory=lambda: f"SPEAKER_{uuid.uuid4().hex[:4].upper()}")
    display_name: str = ""

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.speaker_id

    def to_dict(self) -> dict:
        return {"speaker_id": self.speaker_id, "display_name": self.display_name}