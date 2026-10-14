#!/usr/bin/env python3
# Originally from cognee-integrations (https://github.com/topoteretes/cognee-integrations),
# Copyright 2026 Topoteretes UG, Apache-2.0. Modified by memsift.
"""Trim an ``only_context`` recall item down to the retrieved context (SDK-904).

On cognee >= 1.6.0 a completion search with ``only_context=true`` returns the
whole prompt its own LLM would have received: the conversation history, the
question rendered through the retriever's template, the retrieved context, and
the session guidance block. The host agent already holds the conversation and
needs no answering instructions, so injecting that string whole re-sends what
is already in its context window: about 60% of a mid-session payload was the
agent's own previous turns. This module extracts the retrieved context without an LLM or changes to
what the server retrieved.

Two anchors, both ours to know:

* the question marker ``The question is: `<prompt>```, rendered from the very
  prompt the hook sent, so it matches exactly once however many backticks the
  prompt or the context carry; the context slot opens at the first backtick
  after it, in every one of the server's question templates;
* the end: the final character when the text ends with a backtick, otherwise
  the last ``\\n\\n## Active session guidance`` (the guidance block is the only
  layer the server renders after the template).

Anything that does not fit is left untouched and reported as unparsed: a
template change can only cost tokens, never memory.

The extraction functions are shared by both hosts.
"""

from __future__ import annotations

import re

#: Every cognee question template starts with this line.
QUESTION_PREFIX = "The question is: `"
#: Title of the server-rendered guidance block (session_context_builder.BLOCK_TITLE).
GUIDANCE_TITLE = "## Active session guidance"

PASSAGES_HEADING = "## Relevant passages"
ENTITIES_HEADING = "## Relevant entities"
FACTS_HEADING = "## Related facts"
#: The hybrid retriever joins passages with this, no blank lines around it;
#: a markdown rule inside an answer has them, which ``split_passages`` uses.
PASSAGE_SEPARATOR = "\n---\n"

_SECTION_RE = re.compile(
    r"(?m)^(## Relevant passages|## Relevant entities|## Related facts)[ \t]*\n"
)
_DOC_HEADER_RE = re.compile(r"^(Session ID:|# Session learning\b)")


def split_only_context(text: str, prompt: str) -> dict:
    """Split a 1.6.0 ``only_context`` item into its layers.

    Returns ``{"history", "context", "guidance", "parsed"}``. When the text
    cannot be read as the server's prompt (an older server's bare context, a
    rewritten question, a template this module does not know) ``parsed`` is
    False and the whole text sits in ``context``: fail-open.
    """
    text = str(text or "")
    unparsed = {"history": "", "context": text, "guidance": "", "parsed": False}
    marker = f"{QUESTION_PREFIX}{prompt}`"
    question_at = text.find(marker)
    if question_at < 0:
        return unparsed
    start = text.find("`", question_at + len(marker))
    if start < 0:
        return unparsed
    start += 1
    if text.endswith("`"):
        end = len(text) - 1
        guidance = ""
    else:
        end = text.rfind("`\n\n" + GUIDANCE_TITLE)
        if end < start:
            return unparsed
        guidance = text[end + 1 :].strip()
    return {
        "history": text[:question_at].rstrip(),
        "context": text[start:end],
        "guidance": guidance,
        "parsed": True,
    }


def split_sections(context: str) -> dict:
    """``{"preamble", "passages", "entities", "facts"}``: a key is None when absent.

    ``preamble`` is whatever precedes the first heading (the hybrid retriever's
    optional global-context section, or an unsectioned context from another
    retriever), returned as-is.
    """
    parts = _SECTION_RE.split(context or "")
    sections = {"preamble": parts[0], "passages": None, "entities": None, "facts": None}
    names = {PASSAGES_HEADING: "passages", ENTITIES_HEADING: "entities", FACTS_HEADING: "facts"}
    for i in range(1, len(parts) - 1, 2):
        sections[names[parts[i]]] = parts[i + 1]
    return sections


def split_passages(block: str) -> list[str]:
    """Passages of a ``## Relevant passages`` block.

    The server joins passages with exactly ``\\n---\\n``: the next chunk's text
    starts right after the newline. A markdown horizontal rule inside an
    assistant answer is the same three characters, but it sits between blank
    lines (``\\n\\n---\\n\\n``), so after the split that piece starts with a
    newline. Such a piece is glued back onto the one before it, unless it
    opens with a document header. Documents without a header (remembered text,
    blog posts) start with their own text and stay separate.
    """
    pieces = (block or "").strip("\n").split(PASSAGE_SEPARATOR)
    passages: list[str] = []
    for piece in pieces:
        continuation = piece.startswith("\n") and not _DOC_HEADER_RE.match(piece.lstrip())
        if passages and continuation:
            passages[-1] = passages[-1] + PASSAGE_SEPARATOR + piece
        else:
            passages.append(piece)
    return [p for p in passages if p.strip()]
