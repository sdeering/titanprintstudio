# TITAN Print Studio

Static copy of titanprintstudio.com, kept as a product showcase now that the shop is closed.
Every page, image, font and product video is stored in this repo. All Shopify cart, checkout,
search, account and tracking code has been removed.

## Run locally

```
npx serve -l 8080 .
```

Then open http://localhost:8080

## Rebuild from the live Shopify site

`tools/mirror.py` downloads the pages and assets again and strips Shopify. This only works while
the Shopify store is still online.

```
pip install requests beautifulsoup4
python tools/mirror.py
```

## Hosting

Any static host works: GitHub Pages (deploy from `main`, root), Cloudflare Pages or Netlify.
