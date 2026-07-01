"""Domain terminology glossary routes.

Biases Whisper toward the user's jargon (initial_prompt) and corrects
known mis-hears post-transcription. Seeded with a curated SA / CCaaS /
cloud / sales vocabulary; fully user-editable.

Extracted verbatim from server.py (router split). Same paths, same
handlers, same behavior — see routers/__init__.py.
"""

import asyncio
from typing import Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server import svc

router = APIRouter()


class TerminologyUpdateRequest(BaseModel):
    terms: List[str] = []
    corrections: Dict[str, str] = {}


@router.get("/terminology")
async def get_terminology():
    svc.load_settings()
    if not svc.terminology_svc:
        return {"terms": [], "corrections": {}}
    return await asyncio.to_thread(svc.terminology_svc.get_all)


@router.put("/terminology")
async def put_terminology(req: TerminologyUpdateRequest):
    svc.load_settings()
    if not svc.terminology_svc:
        raise HTTPException(status_code=503, detail="Terminology service not initialized")
    return await asyncio.to_thread(
        svc.terminology_svc.set_all, req.terms, req.corrections)


@router.post("/terminology/reset")
async def reset_terminology():
    svc.load_settings()
    if not svc.terminology_svc:
        raise HTTPException(status_code=503, detail="Terminology service not initialized")
    return await asyncio.to_thread(svc.terminology_svc.reset)
