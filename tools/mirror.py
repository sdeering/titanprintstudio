"""Mirror titanprintstudio.com (Shopify) into a static site with all Shopify code removed.

Usage:  python tools/mirror.py            (run from the repo root)
Requires: requests, beautifulsoup4
"""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit, parse_qsl
from html import unescape

import requests
from bs4 import BeautifulSoup

BASE = "https://titanprintstudio.com"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"}

PRODUCTS = [
    "premium-frame-package-for-sorare-nft-collection",
    "man-city-2023-24-team-signed-shirt-package",
    "aston-villa-2023-24-team-signed-shirt-package",
    "sorare-fridge-magnets",
    "nba-lakers-shirt-nft-display-frame-package",
    "mlb-new-york-yankees-shirt-nft-display-frame-package",
    "england-rugby-world-cup-2023-signed-shirt-nft-display-frame-package",
    "sorare-nft-fridge-magnets-free-promo",
    "arsenal-villa-2023-24-signed-shirt-nft-display-package",
    "liverpool-villa-2023-24-signed-shirt-nft-display-package",
]
COLLECTIONS = ["frontpage", "print-your-sorare-nft-collection", "signed-shirt-packages",
               "custom-frames", "our-products", "all"]
PAGES = ["about", "copy-of-faq", "data-sharing-opt-out"]
POLICIES = ["privacy-policy", "refund-policy", "terms-of-service", "contact-information"]

PATHS = (["/", "/collections", "/blogs/news"]
         + [f"/products/{h}" for h in PRODUCTS]
         + [f"/collections/{h}" for h in COLLECTIONS]
         + [f"/pages/{h}" for h in PAGES]
         + [f"/policies/{h}" for h in POLICIES])

# Theme scripts that keep the site interactive (menus, galleries, video, sliders, animations).
KEEP_JS = {"constants.js", "pubsub.js", "global.js", "details-disclosure.js", "details-modal.js",
           "animations.js", "media-gallery.js", "product-modal.js", "share.js", "show-more.js",
           "scripts.js"}
DROP_CSS = {"component-cart-notification.css", "component-predictive-search.css",
            "component-localization-form.css", "component-pickup-availability.css",
            "component-facets.css", "component-search.css"}

# Asset URLs we mirror locally (first-party CDN only; shopifycloud/app code is dropped).
ASSET_RE = re.compile(
    r"(?:https?:)?//(?:(?:www\.)?titanprintstudio\.com/cdn/(?:shop/(?:files|t/4|videos)|fonts)"
    r"|cdn\.shopify\.com/s/files/1/0904/6284/7285/files)/[^\"'\s,)<>]+")

session = requests.Session()
session.headers.update(UA)
assets = {}  # remote url -> local path (site-root relative, starting with /assets/)


def fetch(url):
    r = session.get(url, timeout=60)
    r.raise_for_status()
    return r


def local_asset(raw):
    """Map a CDN url (as found in HTML, possibly with &amp;) to a local /assets/... path."""
    url = unescape(raw)
    if url.startswith("//"):
        url = "https:" + url
    url = url.replace("http://", "https://")
    parts = urlsplit(url)
    path = parts.path
    if parts.netloc == "cdn.shopify.com":
        rel = "images/" + path.split("/files/", 2)[-1]
    elif path.startswith("/cdn/shop/t/4/assets/"):
        rel = "theme/" + path.split("/cdn/shop/t/4/assets/", 1)[1]
    elif path.startswith("/cdn/shop/t/4/compiled_assets/"):
        rel = "theme/compiled_" + path.split("/compiled_assets/", 1)[1]
    elif path.startswith("/cdn/shop/files/"):
        rel = "images/" + path.split("/cdn/shop/files/", 1)[1]
    elif path.startswith("/cdn/shop/videos/"):
        rel = "videos/" + path.rsplit("/", 1)[1]
    elif path.startswith("/cdn/fonts/"):
        rel = "fonts/" + path.split("/cdn/fonts/", 1)[1]
    else:
        return None
    # Keep image transform params (width, height, crop) in the file name; drop cache/auth params.
    extra = [f"{k}{v}" for k, v in parse_qsl(parts.query)
             if k in ("width", "height", "crop") and v]
    if extra:
        stem, ext = os.path.splitext(rel)
        rel = f"{stem}_{'_'.join(extra)}{ext}"
    local = "/assets/" + rel
    assets[f"https://{parts.netloc}{path}" + (f"?{parts.query}" if parts.query else "")] = local
    return local


def rewrite_assets(text):
    def sub(m):
        loc = local_asset(m.group(0))
        return loc or m.group(0)
    return ASSET_RE.sub(sub, text)


def decompose(nodes):
    for n in list(nodes):
        if n is not None and n.parent is not None:
            n.decompose()


def drop_void(tag):
    """Remove a void tag (link/meta). html.parser sometimes nests following content inside
    these, so keep any children instead of deleting them with the tag."""
    if tag.contents:
        tag.unwrap()
    else:
        tag.decompose()


def strip_shopify(soup, path):
    # --- scripts ---
    for sc in soup.find_all("script"):
        if sc.decomposed:
            continue
        src = sc.get("src")
        if src:
            name = urlsplit(src).path.rsplit("/", 1)[-1]
            if "/cdn/shop/t/4/" not in src or name not in KEEP_JS:
                sc.decompose()
            continue
        stype = sc.get("type", "")
        text = sc.string or ""
        if stype == "application/ld+json":
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    data.pop("offers", None)
                sc.string = json.dumps(data, ensure_ascii=False)
            except ValueError:
                sc.decompose()
            continue
        if "window.shopUrl" in text:
            # Theme strings used by global.js; neutralise cart/search routes.
            text = re.sub(r"window\.routes\s*=\s*\{.*?\};", "window.routes = {};", text, flags=re.S)
            text = re.sub(r"window\.shopUrl\s*=\s*'[^']*';", "", text)
            sc.string = text
            continue
        sc.decompose()

    # --- head links / meta ---
    for link in soup.find_all("link"):
        if link.decomposed:
            continue
        href = link.get("href", "")
        rel = " ".join(link.get("rel", []))
        name = urlsplit(href).path.rsplit("/", 1)[-1]
        if (rel in ("ucp", "preconnect", "dns-prefetch") or "shopify" in href or ".atom" in href
                or link.get("hreflang") or name in DROP_CSS or href.startswith("/cart")
                or (link.get("as") == "script")):
            drop_void(link)
    for meta in soup.find_all("meta"):
        if meta.decomposed:
            continue
        n = (meta.get("name") or meta.get("property") or "")
        if n.startswith("shopify") or n in ("og:price:amount", "og:price:currency"):
            drop_void(meta)

    # --- checkout/wallet styles injected by Shopify ---
    for st in soup.find_all("style"):
        if st.decomposed:
            continue
        css = st.string or ""
        if (st.get("id") or "").startswith("shopify") or "shopify-buyer-consent" in css:
            st.decompose()

    # --- header: cart, account, search, country selector ---
    decompose(soup.select("cart-notification, #cart-notification, #cart-icon-bubble, "
                          ".header__icon--account, account-icon, .menu-drawer__account, "
                          "details-modal.header__search, predictive-search, .header__search, "
                          "localization-form, .localization-wrapper, .desktop-localization-wrapper, "
                          ".menu-drawer__localization, .footer__localization, .footer__payment"))
    # --- product buy area ---
    for pf in soup.select("product-form"):
        btn = BeautifulSoup(
            '<div class="product-form"><div class="product-form__buttons">'
            '<button type="button" class="product-form__submit button button--full-width '
            'button--secondary" disabled aria-disabled="true"><span>Shop closed</span></button>'
            '</div></div>', "html.parser")
        pf.replace_with(btn)
    decompose(soup.select("form.installment, pickup-availability, pickup-availability-preview, "
                          "shopify-accelerated-checkout, div[data-shopify], [class*=bcpo], [id*=bcpo], "
                          "quantity-input, .product-form__quantity, .product-form__input--quantity"))
    for f in soup.select("form.installment"):
        f.decompose()
    # --- collection filters / sorting (needs Shopify backend) ---
    decompose(soup.select(".facets-container, facet-filters-form, #FacetFiltersFormMobile"))
    # --- any remaining store forms ---
    for f in soup.find_all("form"):
        if f.decomposed:
            continue
        act = f.get("action", "")
        if act.startswith(("/cart", "/search", "/localization", "/account", "/contact")):
            f.decompose()
    # --- links to the store backend / Shopify ---
    for a in soup.find_all("a", href=True):
        if a.decomposed:
            continue
        h = a["href"]
        if "shopify.com" in h:
            parent = a.find_parent("small")
            (parent or a).decompose()
        elif re.match(r"^/(cart|account|search|checkouts)", h) or "/cart/" in h:
            a.decompose()
        elif "manager-order-information-form" in h:
            li = a.find_parent("li")
            (li or a).decompose()
    # --- related products: bake in the HTML Shopify would fetch at runtime ---
    for pr in soup.select("product-recommendations"):
        url = (f"{BASE}{pr.get('data-url')}&product_id={pr.get('data-product-id')}"
               f"&section_id={pr.get('data-section-id')}")
        try:
            rsoup = BeautifulSoup(fetch(url).text, "html.parser")
            inner = rsoup.select_one("product-recommendations")
            pr.clear()
            if inner is not None:
                for child in list(inner.contents):
                    pr.append(child.extract())
        except requests.RequestException as e:
            print("  ! recommendations failed:", e)
        pr.name = "div"
        for attr in ("data-url", "data-product-id", "data-section-id"):
            pr.attrs.pop(attr, None)
        strip_shopify_fragment(pr)
    # --- variant pickers: show the options as plain text instead of a selector ---
    for vs in soup.select("variant-selects, variant-radios"):
        lines = []
        for fs in vs.select("fieldset"):
            legend = fs.find("legend")
            name = legend.get_text(" ", strip=True) if legend else "Options"
            values = []
            for inp in fs.select("input[type=radio]"):
                v = inp.get("value")
                if v and v not in values:
                    values.append(v)
            if values:
                lines.append(f"<p><strong>{name}:</strong> {', '.join(values)}</p>")
        for sel in vs.select("select"):
            label = vs.find("label", attrs={"for": sel.get("id")})
            name = label.get_text(" ", strip=True) if label else "Options"
            values = [o.get("value") for o in sel.find_all("option") if o.get("value")]
            if values:
                lines.append(f"<p><strong>{name}:</strong> {', '.join(values)}</p>")
        vs.replace_with(BeautifulSoup(f'<div class="product__text rte">{"".join(lines)}</div>',
                                      "html.parser"))
    decompose(soup.select(".label-unavailable"))

    # --- stock labels: this is a display site, not a shop ---
    for b in soup.select(".badge"):
        if b.decomposed:
            continue
        text = b.get_text(" ", strip=True).lower()
        if "price__badge-sold-out" in b.get("class", []) or "sold out" in text or "available" in text:
            b.decompose()
    for wrap in soup.select(".card__badge"):
        if not wrap.get_text(strip=True):
            wrap.decompose()
    for el in soup.select(".price--sold-out"):
        el["class"] = [c for c in el["class"] if c != "price--sold-out"]
    # HTML comments from Shopify apps
    from bs4 import Comment
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        if "shopify" in c.lower() or "hulk" in c.lower() or "bcpo" in c.lower():
            c.extract()


def strip_shopify_fragment(node):
    for sc in node.find_all("script"):
        sc.decompose()
    for a in node.find_all("a", href=True):
        if a["href"].startswith("/cart"):
            a.decompose()
    for f in node.find_all("form"):
        f.decompose()


def normalise_links(html):
    # absolute same-site links -> root relative
    html = re.sub(r'(href|content)="https?://(?:www\.)?titanprintstudio\.com/(?!cdn/)', r'\1="/', html)
    # /collections/x/products/y -> /products/y
    html = re.sub(r'href="/collections/[^/"]+/products/([^"?#]+)[^"]*"', r'href="/products/\1"', html)
    # strip ?variant= etc. from product links
    html = re.sub(r'href="(/products/[^"?#]+)\?[^"]*"', r'href="\1"', html)
    return html


def out_path(path):
    p = path.strip("/")
    return os.path.join(ROOT, p, "index.html") if p else os.path.join(ROOT, "index.html")


def mirror_page(path):
    print("page", path)
    html = fetch(BASE + path).text
    soup = BeautifulSoup(html, "html.parser")
    strip_shopify(soup, path)
    out = str(soup)
    out = rewrite_assets(out)
    out = normalise_links(out)
    dest = out_path(path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    return out


def download(item):
    url, local = item
    dest = os.path.join(ROOT, local.lstrip("/").replace("/", os.sep))
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest, None
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        r = session.get(url, timeout=300, stream=True)
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
        return dest, None
    except requests.RequestException as e:
        return dest, f"{url}: {e}"


def process_css(css_files):
    """Download relative url() refs (e.g. sparkle.gif) and rewrite absolute CDN refs in CSS."""
    extra = {}
    for local in css_files:
        dest = os.path.join(ROOT, local.lstrip("/").replace("/", os.sep))
        if not os.path.exists(dest):
            continue
        with open(dest, encoding="utf-8", errors="replace") as fh:
            css = fh.read()
        for ref in re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css):
            if ref.startswith(("data:", "#", "http", "//", "/")):
                continue
            remote = urljoin(BASE + "/cdn/shop/t/4/assets/", ref)
            extra[remote] = "/assets/theme/" + urlsplit(ref).path.lstrip("./")
        new = rewrite_assets(css)
        if new != css:
            with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new)
    return extra


def write_static_extras():
    with open(os.path.join(ROOT, ".nojekyll"), "w") as fh:
        fh.write("")
    # 404 page: reuse the home page shell with a simple message
    home = open(out_path("/"), encoding="utf-8").read()
    soup = BeautifulSoup(home, "html.parser")
    main = soup.find("main")
    if main is not None:
        main.clear()
        main.append(BeautifulSoup(
            '<div class="page-width" style="padding:8rem 0;text-align:center">'
            '<h1 class="h1">Page not found</h1>'
            '<p><a class="button" href="/">Back to home</a></p></div>', "html.parser"))
    title = soup.find("title")
    if title is not None:
        title.string = "Page not found – TITAN Print Studio"
    with open(os.path.join(ROOT, "404.html"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(str(soup))


def main():
    for p in PATHS:
        mirror_page(p)
    write_static_extras()
    print(f"\n{len(assets)} assets to download")
    errors = []
    with ThreadPoolExecutor(8) as ex:
        for dest, err in ex.map(download, list(assets.items())):
            if err:
                errors.append(err)
    css = [l for l in set(assets.values()) if l.endswith(".css")]
    extra = process_css(css)
    with ThreadPoolExecutor(8) as ex:
        for dest, err in ex.map(download, list(extra.items())):
            if err:
                errors.append(err)
    print(f"done; {len(errors)} download errors")
    for e in errors:
        print("  !", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
