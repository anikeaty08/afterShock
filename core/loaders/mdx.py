# Aftershock — Core: MDX loader
#
# docs.falkordb.com is Mintlify MDX: Markdown wrapped in JSX components
# (<CodeGroup>, <Accordion>, <Note>, <img/>, style={{...}}). We need the prose
# and fenced code those components wrap, not the components themselves — an
# entity extractor fed raw JSX sees attribute soup, not sentences.
#
# Strategy (SYSTEM_DESIGN.md §7.1 step 2): strip the JSX shell, keep the text
# and code inside it, keep front-matter title/description as metadata, then
# hand the cleaned Markdown to the SDK's own MarkdownLoader parser so heading
# breadcrumbs still work exactly the way StructuralChunking expects.

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import frontmatter
from graphrag_sdk.core.context import Context
from graphrag_sdk.core.exceptions import LoaderError
from graphrag_sdk.core.models import DocumentInfo, DocumentOutput
from graphrag_sdk.ingestion.loaders.base import LoaderStrategy
from graphrag_sdk.ingestion.loaders.markdown_loader import MarkdownLoader

logger = logging.getLogger(__name__)

# Fenced code blocks are sacrosanct: MDX/JSX-looking angle brackets inside an
# example (e.g. a code sample that itself prints `<Foo>`) must never be
# touched by the tag-stripping regexes below. Pull every fence out to a
# placeholder before touching anything else, put it back verbatim after.
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Mintlify components are always capitalized (JSX convention); plain HTML
# tags a docs author might legitimately write (<b>, <sub>, ...) are not
# touched by any of these.
_IMG_TAG_RE = re.compile(r"<img\b([^>]*)/?>", re.IGNORECASE)
_ALT_ATTR_RE = re.compile(r"""alt=["']([^"']*)["']""", re.IGNORECASE)
_TITLE_ATTR_RE = re.compile(r"""title=["']([^"']*)["']""", re.IGNORECASE)
_VOID_COMPONENT_RE = re.compile(r"<[A-Z][A-Za-z0-9]*\b[^>]*/>")
_OPEN_COMPONENT_RE = re.compile(r"<([A-Z][A-Za-z0-9]*)\b([^>]*)>")
_CLOSE_COMPONENT_RE = re.compile(r"</([A-Z][A-Za-z0-9]*)\s*>")
_STYLE_ATTR_RE = re.compile(r"""\s*style=\{\{.*?\}\}""", re.DOTALL)
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# Jekyll-era docs (the FalkorDB/docs history the Replay Lab replays predates
# the Mintlify move): Liquid tags wrap code tabs the way <CodeGroup> does
# later — `{% capture python_0 %}` ... `{% endcapture %}` then
# `{% include code_tabs.html ... %}`. Drop the tags, keep what they capture.
# kramdown attribute lines (`{: .warning }`) are pure styling.
_LIQUID_CAPTURE_OPEN_RE = re.compile(r"\{%-?\s*capture\s+([a-zA-Z]+)(?:_\d+)?\s*-?%\}")
_LIQUID_CAPTURE_CLOSE_RE = re.compile(r"\{%-?\s*endcapture\s*-?%\}")
_LIQUID_TAG_RE = re.compile(r"\{%-?.*?-?%\}", re.DOTALL)
_LIQUID_OUTPUT_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_KRAMDOWN_IAL_RE = re.compile(r"^\{:[^}\n]*\}[ \t]*$", re.MULTILINE)

# Components whose `title=`/`header=` attribute is real content (an FAQ
# question, a step name) worth keeping as a line of text, not just discarding
# with the rest of the tag's attributes.
_TITLE_BEARING_COMPONENTS = frozenset({"Accordion", "Tab", "Step", "Card"})


def _protect_code_fences(text: str) -> tuple[str, list[str]]:
    fences: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    return _CODE_FENCE_RE.sub(_stash, text), fences


def _restore_code_fences(text: str, fences: list[str]) -> str:
    for i, fence in enumerate(fences):
        text = text.replace(f"\x00FENCE{i}\x00", fence)
    return text


def clean_mdx(raw_body: str) -> str:
    """Strip Mintlify JSX (and Jekyll Liquid / kramdown) from a docs body, keeping inner text and code.

    Exposed as a standalone function (not just a private method) so it has a
    focused unit test independent of file I/O — see tests/test_mdx_loader.py.
    """
    # Jekyll code tabs: `{% capture python_0 %}` ... `{% endcapture %}` holds
    # *unfenced* code — left as-is, a `# Output:` comment inside it parses as
    # an H1 and corrupts the heading breadcrumbs. Turn each capture into a
    # real fence first, so it's protected like any other code block below.
    raw_body = _LIQUID_CAPTURE_OPEN_RE.sub(lambda m: f"```{m.group(1)}", raw_body)
    raw_body = _LIQUID_CAPTURE_CLOSE_RE.sub("```", raw_body)

    text, fences = _protect_code_fences(raw_body)

    # <img ... alt="..." /> -> a plain caption line, so alt text (often the
    # only description of a diagram) survives instead of vanishing with the
    # tag. No alt attribute -> the tag is dropped with nothing to keep.
    def _img_to_caption(m: re.Match[str]) -> str:
        attrs = m.group(1)
        alt = _ALT_ATTR_RE.search(attrs)
        return f"\n_{alt.group(1)}_\n" if alt and alt.group(1).strip() else ""

    text = _IMG_TAG_RE.sub(_img_to_caption, text)

    # Other self-closing components (<Frame />, <Divider />, ...) carry no
    # inner text at all — drop the whole tag.
    text = _VOID_COMPONENT_RE.sub("", text)

    # Paired components: drop the tags, keep everything between them. A
    # title-bearing wrapper (Accordion/Tab/Step/Card) gets its title emitted
    # as a bold line first — that's the FAQ question or step name, which is
    # exactly the kind of short factual text worth keeping queryable.
    def _open_to_text(m: re.Match[str]) -> str:
        name, attrs = m.group(1), m.group(2)
        if name in _TITLE_BEARING_COMPONENTS:
            title = _TITLE_ATTR_RE.search(attrs)
            if title and title.group(1).strip():
                return f"\n**{title.group(1)}**\n"
        return ""

    text = _OPEN_COMPONENT_RE.sub(_open_to_text, text)
    text = _CLOSE_COMPONENT_RE.sub("", text)

    # Defensive: a style={{...}} that survived on some other inline element.
    text = _STYLE_ATTR_RE.sub("", text)

    text = _LIQUID_TAG_RE.sub("", text)
    text = _LIQUID_OUTPUT_RE.sub("", text)
    text = _KRAMDOWN_IAL_RE.sub("", text)

    text = _restore_code_fences(text, fences)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip() + "\n"


class MDXLoader(LoaderStrategy):
    """Load an MDX file: strip Mintlify JSX, keep front-matter as metadata,
    delegate structural parsing (heading breadcrumbs) to ``MarkdownLoader``.
    """

    def __init__(self, encoding: str = "utf-8") -> None:
        self.encoding = encoding
        self._md_parser = MarkdownLoader(encoding=encoding)

    async def load(self, source: str, ctx: Context) -> DocumentOutput:
        ctx.log(f"Loading MDX file: {source}")
        return await asyncio.to_thread(self._load_sync, source)

    def _load_sync(self, source: str) -> DocumentOutput:
        path = Path(source)
        if not path.exists():
            raise LoaderError(f"File not found: {source}")

        try:
            raw = path.read_text(encoding=self.encoding)
        except OSError as exc:
            raise LoaderError(f"Failed to read {source}: {exc}") from exc

        post = frontmatter.loads(raw)
        cleaned = clean_mdx(post.content)

        # Reuse the SDK's own structural parser for breadcrumbs — this is the
        # whole point of composing with MarkdownLoader rather than
        # reimplementing heading-hierarchy chunking ourselves.
        elements = self._md_parser._parse_markdown(cleaned)  # noqa: SLF001

        metadata: dict[str, object] = {
            "size_bytes": path.stat().st_size,
            "loader": "mdx",
            "suffix": path.suffix,
        }
        if post.metadata:
            # Front matter's own keys (title, description, sidebarTitle, ...)
            # ride along verbatim under a "frontmatter" key so nothing about
            # loader-internal bookkeeping (size_bytes, suffix) can collide
            # with a page's own front matter field of the same name.
            metadata["frontmatter"] = dict(post.metadata)
            if "title" in post.metadata:
                metadata["title"] = post.metadata["title"]
            if "description" in post.metadata:
                metadata["description"] = post.metadata["description"]

        return DocumentOutput(
            text=cleaned,
            document_info=DocumentInfo(path=str(path), metadata=metadata),
            elements=elements,
        )
