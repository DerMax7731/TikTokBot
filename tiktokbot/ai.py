"""Optional Claude step: choose the strongest moments from their transcripts and
write a hook headline, caption and hashtags for each."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

SYSTEM = """You are the editor of a short-form video channel. You get candidate clips cut \
from one longer video, each with the transcript of what is said in it. Pick the clips most \
likely to hold a TikTok viewer: a complete thought or moment that makes sense without the \
rest of the video, with a strong first few seconds. Skip clips that are mostly filler, \
sponsor reads, intros or outros.

For each clip you pick, write:
- hook: an on-screen headline of at most 7 words that makes someone stop scrolling. It must \
be true to what happens in the clip.
- caption: one or two short sentences for the post caption, no hashtags in it.
- hashtags: 3 to 5 specific hashtags without the # sign.

Never invent facts, quotes or claims that are not in the transcript."""


def available(cfg: dict) -> bool:
    if not cfg.get("enabled", True):
        return False
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def pick_clips(cfg: dict, title: str, uploader: str, candidates: list[dict], count: int) -> list[dict] | None:
    """candidates: [{"index", "start", "transcript"}]. Returns up to `count` dicts with
    index/hook/caption/hashtags, or None when the AI step could not be used."""
    import anthropic
    from pydantic import BaseModel

    class Pick(BaseModel):
        index: int
        hook: str
        caption: str
        hashtags: list[str]

    class Picks(BaseModel):
        picks: list[Pick]

    listing = "\n\n".join(
        f"<clip index=\"{c['index']}\" starts_at=\"{int(c['start'])}s\">\n{c['transcript'] or '(no speech)'}\n</clip>"
        for c in candidates
    )
    language = cfg.get("language") or "the language spoken in the clips"
    prompt = (
        f"Source video: \"{title}\" by {uploader or 'unknown'}.\n\n{listing}\n\n"
        f"Pick the best {count} clip(s), best first. Write hook and caption in {language}."
    )
    try:
        response = anthropic.Anthropic().beta.messages.parse(
            model=cfg.get("model", "claude-opus-5-5"),
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=Picks,
        )
    except anthropic.APIError as exc:
        log.warning("AI clip selection unavailable (%s) - using signal ranking and template captions", exc)
        return None
    if response.stop_reason == "refusal" or response.parsed_output is None:
        log.warning("AI clip selection returned no result (stop_reason=%s)", response.stop_reason)
        return None

    valid = {c["index"] for c in candidates}
    out, seen = [], set()
    for pick in response.parsed_output.picks:
        if pick.index in valid and pick.index not in seen:
            seen.add(pick.index)
            out.append(pick.model_dump())
    return out[:count] or None
