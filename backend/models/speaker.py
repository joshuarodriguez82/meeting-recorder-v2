"""Speaker identity model.

A Speaker represents one person heard in a given session's audio. It
exists in two forms:

  - The transient ID the diarizer hands out per session (e.g. "SPEAKER_00",
    "SPEAKER_01"). These are not stable across recordings — Monday's
    "SPEAKER_00" is not necessarily Tuesday's "SPEAKER_00".

  - A persistent SpeakerProfile (see services/speaker_profile_service.py)
    that links a display name like "John Smith" to a voice fingerprint
    embedding. After the user labels SPEAKER_00 once as John, subsequent
    sessions can auto-match a new speaker's centroid against the saved
    profile and pre-fill the name.

Speaker on a Session carries the link back to a profile via
`profile_id`, plus the confidence and a confirmation flag so the UI can
show "Likely John (87%) — confirm?" until the user accepts or rejects.
"""

from dataclasses import dataclass, field
from typing import List, Optional
import uuid


@dataclass
class Speaker:
    """Represents a detected speaker in a meeting session."""

    speaker_id: str = field(default_factory=lambda: f"SPEAKER_{uuid.uuid4().hex[:4].upper()}")
    display_name: str = ""

    # ── Cross-session identity (added in v2.2.0 with #5) ──────────────
    # profile_id links this session-local speaker to a SpeakerProfile in
    # the profiles store; None until either the user manually renames
    # the speaker (which creates a new profile) or the auto-match wins.
    profile_id: Optional[str] = None

    # Cosine similarity of this speaker's centroid against the matched
    # profile (0.0–1.0). Only set when an auto-match fires; None when
    # the user renamed manually or when no match was found at all.
    match_confidence: Optional[float] = None

    # True after the user has explicitly confirmed (or done a manual
    # rename, which counts). False on auto-matches that haven't been
    # reviewed yet — the UI uses this to render a "(87%) confirm?" badge.
    match_confirmed: bool = False

    # 192-dim ECAPA centroid embedding for THIS session's audio for THIS
    # speaker label, L2-normalized. Persisted so that:
    #   1. A manual rename later can create a profile without re-running
    #      diarization just to compute the centroid.
    #   2. The Settings → Known Speakers UI can offer a "merge" action
    #      based on any session, even if its profile is empty.
    # Empty list when diarization hasn't run yet for this session.
    embedding: List[float] = field(default_factory=list)

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.speaker_id

    def to_dict(self) -> dict:
        return {
            "speaker_id": self.speaker_id,
            "display_name": self.display_name,
            "profile_id": self.profile_id,
            "match_confidence": self.match_confidence,
            "match_confirmed": self.match_confirmed,
            "embedding": list(self.embedding) if self.embedding else [],
        }
