# WAYBOX → monomarket feeds

Every 15 minutes GitHub Actions reads the public site feed
`https://waybox.com.ua/feeds/monomarket.xml` (read-only) and publishes to GitHub Pages:

- `products.xml` — product feed in monomarket `<Market>` format
- `offers.json` — price list: price, old price, stock, warranty, days to dispatch

If the site feed is unavailable or broken, the run fails and the previously published files stay online.

Settings are in `config.json`:
- `warranty_type`, `warranty_period_default` (used only when a product has no «Гарантія, міс»)
- `days_to_dispatch` (0 today, 1 tomorrow)
- `max_pay_in_parts` (null = monomarket default)
- `package_by_category` — package weight (kg) and size (cm) by site category id
- `barcodes` — `{"product id": "EAN"}` when barcodes become available

Which products are included is set on the site: OkayCMS → Feeds → «Monomarket» (brands, price > 1999, in stock).
