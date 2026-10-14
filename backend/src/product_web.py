"""Parse public product pages without asking the AI to discover URLs or images."""

from __future__ import annotations

import html
import json
import re
import urllib.parse

# The frozen runtime does not include html.parser or its _markupbase dependency.
# Bundle the CPython parser with the app so source and packaged runs use the same code.
from product_html_parser import HTMLParser


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def context(self):
        parts, node = [], self
        while node:
            parts.append(
                str(node.attrs.get("class") or "")
                + " "
                + str(node.attrs.get("id") or "")
            )
            node = node.parent
        return " ".join(parts).lower()

    def text(self):
        if self.tag in ("script", "style", "nav", "footer", "header", "form"):
            return ""
        classes = set(str(self.attrs.get("class") or "").lower().split())
        if classes.intersection(
            {"related", "upsells", "sidebar", "widget", "woocommerce-tabs-nav"}
        ):
            return ""
        return " ".join(
            child.text() if isinstance(child, Node) else child
            for child in self.children
        )


class PageParser(HTMLParser):
    VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def canonical_url(value, base):
    """Drop fragments/tracking and reject cart, filter, search and login actions."""
    try:
        parts = urllib.parse.urlsplit(
            urllib.parse.urljoin(base, html.unescape(str(value or "")).strip())
        )
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
        ):
            return ""
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if any(
            k.lower()
            in {"add-to-cart", "remove_item", "orderby", "s", "redirect_to", "action"}
            or k.lower().startswith(("filter_", "query_type_"))
            for k, _ in query
        ):
            return ""
        query = [
            (k, v)
            for k, v in query
            if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
        ]
        path = re.sub(r"/page/1/?$", "/", parts.path or "/")
        return urllib.parse.urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                path,
                urllib.parse.urlencode(sorted(query)),
                "",
            )
        )
    except ValueError:
        return ""


def listing_url(url):
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lower().strip("/")
    return bool(
        re.search(
            r"(?:^|/)(?:danh-muc|product-category|category|collections)(?:/|$)", path
        )
        or re.fullmatch(r"(?:san-pham|products?|shop)(?:/page/\d+)?", path)
        or re.search(r"/page/\d+$", path)
    )


def _schema_products(value):
    if isinstance(value, list):
        for item in value:
            yield from _schema_products(item)
    elif isinstance(value, dict):
        types = value.get("@type") or []
        if isinstance(types, str):
            types = [types]
        if any(str(t).rsplit("/", 1)[-1] == "Product" for t in types):
            yield value
        for key in ("@graph", "mainEntity"):
            if key in value:
                yield from _schema_products(value[key])


def parse_product_page(raw_html, base_url, limit=300):
    root = PageParser(raw_html).root
    nodes = list(root.walk())
    title = next(
        (
            n
            for n in nodes
            if n.tag == "h1"
            and re.search(r"product[_-]title", str(n.attrs.get("class") or ""))
        ),
        None,
    )
    schemas = []
    for node in nodes:
        if node.tag == "script" and node.attrs.get("type") == "application/ld+json":
            try:
                schemas.extend(
                    _schema_products(
                        json.loads(
                            "".join(c for c in node.children if isinstance(c, str))
                        )
                    )
                )
            except (ValueError, TypeError):
                pass
    # Only a single product schema represents a detail page; ItemList is a listing.
    schema = schemas[0] if len(schemas) == 1 and not listing_url(base_url) else {}
    og_product = any(
        n.tag == "meta"
        and n.attrs.get("property") == "og:type"
        and str(n.attrs.get("content") or "").lower() in {"product", "og:product"}
        for n in nodes
    )
    detail = bool(title or schema or og_product)
    if detail and title is None:
        title = next((n for n in nodes if n.tag == "h1"), None)
    name = " ".join((title.text() if title else str(schema.get("name") or "")).split())
    body = ""
    if detail:
        sections = [
            n
            for n in nodes
            if n.attrs.get("id") in {"tab-description", "tab-additional_information"}
            or "short-description" in str(n.attrs.get("class") or "")
            or "woocommerce-Tabs-panel--description" in str(n.attrs.get("class") or "")
            or (
                "accordion-inner" in str(n.attrs.get("class") or "")
                and any(
                    marker in n.context()
                    for marker in (
                        "product-page-accordian",
                        "product-page-accordion",
                        "product-footer",
                    )
                )
            )
        ]
        if sections:
            body = "\n".join([name] + [" ".join(n.text().split()) for n in sections])
        else:
            main = next((n for n in nodes if n.tag == "main"), root)
            body = " ".join(main.text().split())

    images = []

    def add_image(value):
        if isinstance(value, list):
            for v in value:
                add_image(v)
            return
        if isinstance(value, dict):
            value = value.get("url") or value.get("contentUrl") or ""
        url = canonical_url(value, base_url) if value else ""
        if url and url not in images:
            images.append(url)

    if detail:
        for n in nodes:
            if (
                n.tag == "img"
                and not re.search(r"(?:^|\s)(?:related|upsells)(?:\s|$)", n.context())
                and (
                    "woocommerce-product-gallery" in n.context()
                    or "product-gallery" in n.context()
                    or n.attrs.get("itemprop") == "image"
                    or "wp-post-image" in str(n.attrs.get("class") or "")
                )
            ):
                for key in (
                    "data-large_image",
                    "data-src",
                    "data-lazy-src",
                    "data-original",
                    "src",
                ):
                    value = n.attrs.get(key)
                    if value and not str(value).startswith("data:"):
                        add_image(value)
                        break
        add_image(schema.get("image"))
        for n in nodes:
            if n.tag == "meta" and (n.attrs.get("property") or n.attrs.get("name")) in {
                "og:image",
                "og:image:url",
            }:
                add_image(n.attrs.get("content"))

    home = (urllib.parse.urlsplit(base_url).hostname or "").lower().removeprefix("www.")
    links = {}
    for n in nodes:
        if n.tag != "a":
            continue
        href = str(n.attrs.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = canonical_url(href, base_url)
        if not url or url.rstrip("/") == canonical_url(base_url, base_url).rstrip("/"):
            continue
        parts = urllib.parse.urlsplit(url)
        if (parts.hostname or "").lower().removeprefix("www.") != home:
            continue
        path = urllib.parse.unquote(parts.path).lower()
        if path == "/" or re.search(
            r"\.(?:png|jpe?g|gif|webp|svg|avif|ico|css|js|pdf|zip|woff2?|mp4|mp3)$",
            path,
        ):
            continue
        if any(
            v in path
            for v in ("/cart", "/checkout", "/my-account", "/gio-hang", "/tai-khoan")
        ):
            continue
        context = n.context()
        hint = any(
            v in path
            for v in ("/san-pham", "/product", "/danh-muc", "/shop", "/collections")
        )
        pagination = (
            "page-numbers" in context
            or "pagination" in context
            or n.attrs.get("rel") == "next"
        )
        card = bool(
            re.search(
                r"(?:^|\s)(?:product|product-small|product-card|product-item)(?:\s|$)",
                context,
            )
        )
        if hint or card or pagination:
            links.setdefault(url, 0 if listing_url(url) or pagination else 1)
    return {
        "product_page": detail,
        "product_name": name,
        "product_text": body,
        "image_urls": images[:4],
        "links": sorted(links, key=links.get)[:limit],
    }
