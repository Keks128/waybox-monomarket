"""WAYBOX -> monomarket feeds.

Reads the public site feed (OkayCMS Feeds, Rozetka preset) and writes:
  public/products.xml  - product content feed in monomarket <Market> format
  public/offers.json   - price list (prices, stock, warranty) in monomarket format
Only standard library. Nothing is written to the site, it is read-only.
If the source looks broken, the script exits with an error and the previously
published files stay as they are.
"""
import datetime as dt
import hashlib
import html
import shutil
import time
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
OUT = os.path.join(HERE, "public")
MIN_OFFERS = CFG.get("min_offers", 20)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "waybox-monomarket-feed/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def txt(el):
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


ALLOWED = {"p", "ul", "li", "br", "h5", "img"}


def clean_description(raw):
    """Keep only tags monomarket allows (h5, p, ul, li, br, img); headings -> h5, other tags unwrapped."""
    s = raw or ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", s)
    s = re.sub(r"(?i)<(/?)h[1-6][^>]*>", r"<\1h5>", s)
    s = re.sub(r"(?i)<ol[^>]*>", "<ul>", s)
    s = re.sub(r"(?i)</ol>", "</ul>", s)
    s = re.sub(r"(?i)</?(td|th)[^>]*>", " ", s)
    s = re.sub(r"(?i)</tr>", "<br>", s)

    def tag(m):
        close, name, rest = m.group(1), m.group(2).lower(), m.group(3)
        if name not in ALLOWED:
            return ""
        if name == "img":
            src = re.search(r'src\s*=\s*"([^"]+)"', rest, re.I) or re.search(r"src\s*=\s*'([^']+)'", rest, re.I)
            if not src:
                return ""
            u = src.group(1)
            if u.startswith("/"):
                u = CFG["site"] + u
            return '<img alt="" src="%s">' % u
        if name == "br":
            return "<br>"
        return "<%s%s>" % (close, name)

    s = re.sub(r"(?s)<(/?)([a-zA-Z0-9]+)([^>]*)>", tag, s)
    s = re.sub(r"(<br>\s*){3,}", "<br><br>", s)
    s = re.sub(r"<(p|li|h5)>\s*</\1>", "", s)
    return s.replace("]]>", "]] >").strip()


def clean_title(name, vendor_code):
    t = html.unescape(name)
    t = re.sub(r"\s*(?<!\d),(?!\d)\s*", " ", t)   # monomarket: no commas in titles (keep 3,5 кВт)
    t = re.sub(r"\s+[–—-]\s+", " ", t)              # no dashes between words
    t = re.sub(r"\s{2,}", " ", t).strip()
    w = t.split()
    for i in range(3, len(w) - 2):                 # site feed appends variant name that repeats the product name
        if w[i:i + 3] == w[0:3]:
            t = " ".join(w[:i])
            break
    limit = 100
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0]
    return t


def param_values(p):
    vals = [txt(v) for v in p.findall("value")]
    if not vals:
        vals = [txt(p)]
    return [v for v in vals if v]


CACHE = os.path.join(HERE, "img_cache")
IMG_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
_downloads = {"new": 0, "failed": 0}


def mirror_picture(url):
    """Copy a site photo to our own hosting once (cached by URL), return the public URL or None.
    Already cached photos are never downloaded again, so the site gets almost no extra load."""
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    for ext in IMG_EXT.values():
        if os.path.exists(os.path.join(CACHE, key + "." + ext)):
            return key + "." + ext
    if _downloads["new"] >= CFG.get("max_new_images_per_run", 400):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "waybox-monomarket-feed/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            data = r.read()
    except Exception:
        _downloads["failed"] += 1
        return None
    ext = IMG_EXT.get(ctype)
    if not ext or len(data) < 1024:
        _downloads["failed"] += 1
        return None
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, key + "." + ext), "wb") as f:
        f.write(data)
    _downloads["new"] += 1
    time.sleep(CFG.get("download_pause_sec", 0.3))
    return key + "." + ext


def pkg(cat_id, title="", brand=""):
    """Real package sizes from the client (package_rules, first match by title/brand wins),
    otherwise the old estimate by category."""
    for r in CFG.get("package_rules", []):
        if r.get("brand") and r["brand"].lower() != (brand or "").lower():
            continue
        if re.search(r["match"], title or "", re.I):
            base = CFG["package_by_category"].get(str(cat_id)) or CFG["package_default"]
            return {**base, **r["pkg"]}
    return CFG["package_by_category"].get(str(cat_id)) or CFG["package_default"]


def main():
    raw = fetch(CFG["source_feed"])
    root = ET.fromstring(raw)
    cats = {c.get("id"): txt(c) for c in root.iter("category")}
    offers = list(root.iter("offer"))
    if len(offers) < MIN_OFFERS:
        sys.exit("source feed has only %d offers (< %d), keeping previous files" % (len(offers), MIN_OFFERS))

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Market>", "<offers>"]
    price_rows = []
    seen = set()
    used_images = set()
    skipped = []
    for o in offers:
        oid = o.get("id")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        try:
            price = int(round(float(txt(o.find("price")) or 0)))
        except ValueError:
            continue
        if price <= 0:
            continue
        try:
            stock = max(0, int(float(txt(o.find("stock_quantity")) or 0)))
        except ValueError:
            stock = 0
        old = txt(o.find("old_price"))
        old_price = int(round(float(old))) if old else None
        if old_price is not None and old_price <= price:
            old_price = None
        params = {}
        for p in o.findall("param"):
            params.setdefault(p.get("name"), []).extend(param_values(p))
        vendor_code = (params.get("Артикул") or [""])[0].strip()
        brand = txt(o.find("vendor")) or (params.get("Бренд") or [""])[0]
        cat_id = txt(o.find("categoryId"))
        title = clean_title(txt(o.find("name")), vendor_code)
        desc = clean_description(txt(o.find("description_ua")) or txt(o.find("description")))
        src_pics = [txt(x) for x in o.findall("picture") if txt(x)]
        checked = CFG.get("photos", {}).get(oid)
        if checked is not None:
            # only photos reviewed as white background, matched by file name (order on the site may change)
            src_pics = [u for u in src_pics if u.rsplit("/", 1)[-1] in checked]
        if not src_pics or (checked is None and CFG.get("skip_products_without_checked_photos")):
            skipped.append(oid)
            continue
        src_pics = src_pics[:CFG.get("pictures_per_product", 3)]
        pics = []
        for u in src_pics:
            name = mirror_picture(u) if CFG.get("mirror_images", True) else None
            if name:
                used_images.add(name)
                pics.append(CFG["images_base_url"].rstrip("/") + "/img/" + name)
            else:
                pics.append(u)
        warranty = None
        for k in ("Гарантія, міс", "Гарантія"):
            if params.get(k):
                m = re.search(r"\d+", params[k][0])
                if m:
                    warranty = int(m.group())
                    break
        if warranty is None:
            warranty = CFG["warranty_period_default"]
        pk = pkg(cat_id, title, brand)
        available = stock > 0

        x = ["<offer>",
             "<id>%s</id>" % escape(oid),
             "<code>%s</code>" % escape(oid),
             "<vendor_code>%s</vendor_code>" % escape(vendor_code),
             "<title>%s</title>" % escape(title)]
        bc = CFG.get("barcodes", {}).get(oid)
        if bc:
            x.append("<barcode>%s</barcode>" % escape(bc))
        x += ["<category>%s</category>" % escape(cats.get(cat_id, "")),
              "<brand>%s</brand>" % escape(brand),
              "<availability>%s</availability>" % ("Є в наявності" if available else "Немає в наявності"),
              "<weight>%s</weight>" % pk["weight"],
              "<height>%s</height>" % pk["height"],
              "<width>%s</width>" % pk["width"],
              "<length>%s</length>" % pk["length"],
              "<description><![CDATA[%s]]></description>" % desc,
              "<image_link>"] + ["<picture>%s</picture>" % escape(u) for u in pics] + ["</image_link>", "<tags>"]
        for name, vals in params.items():
            if name in ("Бренд",):
                continue
            if name in CFG.get("split_params", []):
                vals = [v.strip() for s in vals for v in s.split(",") if v.strip()]
            for v in vals:
                x.append('<param name="%s">%s</param>' % (escape(name, {'"': "&quot;"}), escape(v)))
        x += ["</tags>", "</offer>"]
        xml_parts.append("\n".join(x))

        row = {"code": oid, "price": price, "old_price": old_price, "availability": available,
               "stock": stock, "warehouses": None,
               "warranty_type": CFG["warranty_type"], "warranty_period": warranty,
               "days_to_dispatch": CFG["days_to_dispatch"], "manufacture": None}
        if CFG.get("max_pay_in_parts"):
            row["max_pay_in_parts"] = CFG["max_pay_in_parts"]
        price_rows.append(row)

    xml_parts += ["</offers>", "</Market>"]
    if len(price_rows) < MIN_OFFERS:
        sys.exit("only %d valid offers, keeping previous files" % len(price_rows))
    os.makedirs(OUT, exist_ok=True)
    xml_text = "\n".join(xml_parts)
    ET.fromstring(xml_text.encode("utf-8"))  # must be valid XML, otherwise fail before writing
    with open(os.path.join(OUT, "products.xml"), "w", encoding="utf-8") as f:
        f.write(xml_text)
    with open(os.path.join(OUT, "offers.json"), "w", encoding="utf-8") as f:
        json.dump({"updatedAt": now, "total": len(price_rows), "data": price_rows}, f, ensure_ascii=False, indent=1)
    img_out = os.path.join(OUT, "img")
    os.makedirs(img_out, exist_ok=True)
    for name in sorted(used_images):
        shutil.copyfile(os.path.join(CACHE, name), os.path.join(img_out, name))
    if os.path.isdir(CACHE):
        for name in os.listdir(CACHE):          # drop photos no longer used in the feed
            if name not in used_images and name != "manifest.txt":
                os.remove(os.path.join(CACHE, name))
        with open(os.path.join(CACHE, "manifest.txt"), "w") as f:
            f.write("\n".join(sorted(used_images)))
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8"><title>WAYBOX monomarket feeds</title>'
                '<p><a href="products.xml">products.xml</a> | <a href="offers.json">offers.json</a> | updated %s</p>' % now)
    print("skipped (no checked white photo): %s" % ", ".join(skipped))
    print("ok: %d offers, %d images (%d new, %d failed), updatedAt %s"
          % (len(price_rows), len(used_images), _downloads["new"], _downloads["failed"], now))


if __name__ == "__main__":
    main()
