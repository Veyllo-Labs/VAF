# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Clean HTML that came from outside before anything renders it.

One sanitizer for every producer of HTML the web UI puts into a page: mail bodies (a sender
wrote them) and research sections (a model wrote them from web pages, so a page it read can
steer them). Both used to be the mail layer's own ``nh3`` call or nothing at all; the research
paper rendered model output into the app's document with no filter, where an ``onerror``
attribute runs as the viewer.

The policy is data, the call is shared: a consumer passes its own allowlists and, if it needs
one, an attribute filter (mail rewrites ``cid:`` images and gates remote ones). The defaults
are the DOCUMENT policy - structure, text and links, nothing that runs, loads or styles.

Named boundary: this is the trust boundary on the server. The web UI adds a second layer of
its own (``sanitizeUntrustedHtml`` in web/lib/sanitize.ts, the CSP in the mail frame), because
a sanitizer bug must not become an execution.
"""
from __future__ import annotations

from typing import Callable, Mapping, Optional

# Structure and text. Deliberately without img (a remote image in a report is a request the
# reader did not ask for), without style/class/id (in the app's document they draw over the
# real interface or shadow its globals), and without anything that runs or embeds.
DOCUMENT_TAGS = frozenset({
    "a", "abbr", "b", "blockquote", "br", "caption", "cite", "code", "col", "colgroup",
    "dd", "del", "dfn", "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "ins", "kbd", "li", "mark", "ol", "p", "pre", "q", "s",
    "samp", "small", "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th",
    "thead", "time", "tr", "u", "ul", "var",
})
DOCUMENT_ATTRIBUTES: Mapping[str, frozenset] = {
    "a": frozenset({"href", "title"}),
    "abbr": frozenset({"title"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "ol": frozenset({"start"}),
    "time": frozenset({"datetime"}),
}
SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto"})


def sanitize_html(
    dirty: Optional[str],
    *,
    tags=DOCUMENT_TAGS,
    attributes: Mapping[str, object] = DOCUMENT_ATTRIBUTES,
    attribute_filter: Optional[Callable[[str, str, str], Optional[str]]] = None,
    url_schemes=SAFE_URL_SCHEMES,
    link_rel: str = "noopener noreferrer nofollow",
) -> str:
    """``dirty`` cleaned to the given allowlists (the DOCUMENT policy by default).

    Script and style CONTENT is dropped, not kept as text; every other disallowed tag is
    removed and its text kept, so a report stays readable when the model used a tag the
    policy does not know.
    """
    import nh3

    return nh3.clean(
        dirty or "",
        tags=set(tags),
        attributes={name: set(values) for name, values in attributes.items()},
        attribute_filter=attribute_filter,
        link_rel=link_rel,
        url_schemes=set(url_schemes),
    )
