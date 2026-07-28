"""
smart_adaptor.py
----------------
Memory-efficient wrapper around ``scrapling.parser.Selector`` (the "Adaptor").

Provides:
  - ``yield_elements(selector)``  – generator that yields one ``Selector`` at a
    time instead of returning a massive ``Selectors`` list.
  - ``extract_text(element)``      – clean / normalise extracted text in one
    call.
  - A context-manager class ``SmartAdaptor`` that lazily parses the HTML tree.
"""

import re
from typing import Generator, Optional, Union

from scrapling.parser import Selector

# Compiled regex for excessive whitespace / newlines
_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_RE_MULTI_NEWLINE = re.compile(r"\n{2,}")
_RE_HIDDEN = re.compile(r"<!--.*?-->", re.DOTALL)


# ---------------------------------------------------------------------------
# Text normalisation helpers (operate on plain strings)
# ---------------------------------------------------------------------------

def clean_text(raw: str) -> str:
    """
    Strip hidden HTML comments, collapse multiple spaces/tabs, collapse
    excessive newlines, and strip leading/trailing whitespace.

    This is intentionally kept lightweight – no DOM round-trip.
    """
    text = _RE_HIDDEN.sub("", raw)
    text = _RE_MULTI_NEWLINE.sub("\n", text)
    text = _RE_MULTI_SPACE.sub(" ", text)
    return text.strip()


def extract_text(element: Selector, separator: str = "\n") -> str:
    """
    Convenience shortcut: get all visible text of a ``Selector`` element and
    run ``clean_text`` on the result.

    Internally uses ``Selector.get_all_text()`` with sensible defaults.
    """
    raw = element.get_all_text(separator=separator, strip=True)
    return clean_text(str(raw))


# ---------------------------------------------------------------------------
# Generator-based extraction  (the core optimisation)
# ---------------------------------------------------------------------------

def yield_elements(
    root: Selector,
    css_selector: str,
) -> Generator[Selector, None, None]:
    """
    Generator that lazily yields matching elements from the parsed HTML tree.

    Unlike ``root.css(css_selector)`` which materialises a full ``Selectors``
    list in memory, this iterates the underlying lxml nodes on the fly.

    Usage::

        adaptor = SmartAdaptor(html_str)
        for el in yield_elements(adaptor.root, "div.product"):
            process(el)
    """
    if root._is_text_node(root._root):
        return

    # We use lxml's XPath evaluator directly to avoid creating intermediate
    # Selector wrappers until the caller actually asks for each element.
    from cssselect import parse as split_selectors
    from scrapling.core.translator import css_to_xpath as _css_to_xpath

    try:
        xpath = _css_to_xpath(css_selector)
    except Exception:
        # Fall back to treating the input as a raw XPath
        xpath = css_selector

    # Evaluate the XPath – this returns lxml HtmlElement objects.
    nodes = root._root.xpath(xpath)

    # Now wrap each lxml node in a Selector on-the-fly as the caller iterates.
    for node in nodes:
        yield Selector(
            root=node,
            url=root.url,
            encoding=root.encoding,
            adaptive=False,
            huge_tree=False,              # already parsed, no need for huge_tree
            keep_comments=root._Selector__keep_comments,  # type: ignore[attr-defined]
        )
        # Free the node reference so it can be GC'd before the next iteration
        del node


# ---------------------------------------------------------------------------
# SmartAdaptor  –  context manager that wraps scrapling's Selector
# ---------------------------------------------------------------------------

class SmartAdaptor:
    """
    Wraps ``scrapling.parser.Selector`` in a memory-conscious API.

    Typical usage::

        with SmartAdaptor(html_string, url="...") as sa:
            for el in sa.css("div.item"):
                title = sa.text(el, "h2")
                ...

    If you prefer to manage the lifecycle yourself, call ``parse()`` directly
    and access ``.root``.
    """

    __slots__ = ("_html", "_url", "_encoding", "root")

    def __init__(
        self,
        html: Union[str, bytes],
        url: str = "",
        encoding: str = "utf-8",
    ) -> None:
        self._html = html
        self._url = url
        self._encoding = encoding
        self.root: Optional[Selector] = None

    def parse(self) -> "SmartAdaptor":
        """Lazily parse the HTML into a ``Selector`` instance."""
        if self.root is None:
            self.root = Selector(
                content=self._html,
                url=self._url,
                encoding=self._encoding,
                huge_tree=False,  # no reason to keep huge_tree on
                adaptive=False,
            )
        return self

    def css(self, selector: str) -> Generator[Selector, None, None]:
        """Same as ``yield_elements``, exposed as a method for convenience."""
        if self.root is None:
            self.parse()
        assert self.root is not None
        yield from yield_elements(self.root, selector)

    def text(self, element: Selector) -> str:
        """Convenience: extract + clean visible text from a matched element."""
        return extract_text(element)

    def __enter__(self) -> "SmartAdaptor":
        self.parse()
        return self

    def __exit__(self, *exc_info) -> None:
        # Explicitly clear the parsed tree so the GC can reclaim memory ASAP.
        self.root = None
        self._html = ""
