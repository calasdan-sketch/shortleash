"""Cache optimizer: prompts structured for LLM prompt caching.

Prompt caching (Anthropic/OpenAI/Gemini/DeepSeek all do some form) discounts
repeated prefix tokens — up to 90% off on cache hits. But it only works if
the stable content sits FIRST and the per-request variables sit LAST.
Most hand-written prompts interleave them, silently killing the cache.

`cache make` restructures a {{variable}}-templated prompt into
cache-optimal layout and emits per-provider integration snippets.
`cache check` reports how cacheable a prompt is. `cache guide` prints
the verified provider docs.
"""

from __future__ import annotations

import re

PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
# \w (not [a-zA-Z0-9_]) so unicode names like {{naïve}} are found too.

CACHE_DOCS = [
    ("Anthropic",
     "https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching",
     "Explicit breakpoints via cache_control; up to 90% off cached tokens. "
     "Put static content in early blocks, mark the breakpoint."),
    ("OpenAI",
     "https://platform.openai.com/docs/guides/prompt-caching",
     "Automatic prefix caching (no code changes); 50% off. "
     "Keep the stable prefix byte-identical across requests."),
    ("Google Gemini",
     "https://ai.google.dev/gemini-api/docs/caching",
     "Explicit context caching API (cached_content); 75% off. "
     "Cache the stable context object, pass variables per request."),
    ("DeepSeek",
     "https://api-docs.deepseek.com/guides/kv_cache",
     "Automatic prefix caching on repeated prefixes; 50% off. "
     "Stable prefix first, same as OpenAI."),
]


def find_placeholders(text: str) -> list[str]:
    seen: list[str] = []
    for m in PLACEHOLDER_RE.finditer(text):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def analyze(text: str) -> dict:
    placeholders = find_placeholders(text)
    static = PLACEHOLDER_RE.sub("", text)
    return {
        "placeholders": placeholders,
        "n_placeholders": len(placeholders),
        "static_chars": len(static),
        "total_chars": len(text),
        "static_ratio": len(static) / len(text) if text else 1.0,
    }


def restructure(text: str) -> tuple[str, list[str]]:
    """Return (optimized_text, notes). Static content first (cacheable
    prefix), variables last (below the breakpoint)."""
    placeholders = find_placeholders(text)
    notes: list[str] = []
    if not placeholders:
        notes.append("no {{variables}} found — the whole prompt is static, "
                     "already fully cacheable.")
        return text, notes
    # Split into static segments, preserving order, dropping empties.
    parts = PLACEHOLDER_RE.split(text)
    statics = [p for i, p in enumerate(parts) if i % 2 == 0 and p.strip()]
    static_block = "\n".join(s.rstrip() for s in statics).strip()
    var_block = "\n".join("{{" + name + "}}" for name in placeholders)
    optimized = (static_block
                 + "\n\n---\n"
                   "Variables (per-request — keep below the cache breakpoint):\n"
                 + var_block + "\n")
    # Warn if a variable was buried mid-prompt (the thing we're fixing).
    first_var = text.find("{{")
    if first_var > len(text) // 2:
        notes.append("variables were already near the end — good shape, "
                     "minor cleanup applied.")
    else:
        notes.append(f"moved {len(placeholders)} variable(s) below the "
                     "static prefix so the cache hits.")
    notes.append("Review the rewrite: reordering can change emphasis. "
                 "The static text is preserved verbatim.")
    return optimized, notes


def provider_snippet(provider: str) -> str:
    p = provider.lower()
    if p == "anthropic":
        return '''Anthropic (explicit breakpoint):
  import anthropic
  client = anthropic.Anthropic()
  # Put the STATIC part in its own block with cache_control.
  # Everything after the breakpoint (your {{variables}}) is fresh each call.
  msg = client.messages.create(
      model="claude-opus-4-6",
      max_tokens=1024,
      system=[{"type": "text", "text": STATIC_PROMPT,
               "cache_control": {"type": "ephemeral"}}],
      messages=[{"role": "user",
                 "content": VARIABLES_TEXT}])
  # Cached input tokens are ~90% cheaper. Breakpoints: max 4, min 1024 tokens.'''
    if p == "openai":
        return '''OpenAI (automatic — no code changes):
  # Prefix caching is automatic on repeated prefixes (50% off).
  # Just keep the STATIC part byte-identical and FIRST in messages:
  messages = [{"role": "system", "content": STATIC_PROMPT},   # stable prefix
              {"role": "user", "content": VARIABLES_TEXT}]    # changes per call'''
    if p == "gemini":
        return '''Gemini (explicit context caching, 75% off):
  from google import genai
  client = genai.Client()
  # Cache the stable context once, reuse its name per request:
  cache = client.caches.create(model="gemini-2.5-flash",
                                config={"contents": STATIC_PROMPT,
                                        "ttl": "3600s"})
  resp = client.models.generate_content(
      model="gemini-2.5-flash",
      config={"cached_content": cache.name},
      contents=VARIABLES_TEXT)'''
    if p == "deepseek":
        return '''DeepSeek (automatic prefix caching, 50% off):
  # Same rule as OpenAI: stable prefix first, byte-identical across calls.
  # No API changes needed — the cache engages on prefix match.'''
    raise ValueError(f"unknown provider: {provider!r}")


def guide_text() -> str:
    lines = ["", "  PROMPT CACHING — provider guide", "",
             "  Cache discounts repeated prefix tokens. The rule everywhere:"]
    lines.append("  STATIC content first (system prompt, docs, examples),")
    lines.append("  {{variables}} last. Byte-identical prefixes hit the cache.")
    lines.append("")
    for name, url, note in CACHE_DOCS:
        lines.append(f"  {name}\n    {url}\n    {note}\n")
    lines.append("  `shortleash cache make -i prompt.txt` restructures")
    lines.append("  your prompt for this automatically.")
    return "\n".join(lines)
