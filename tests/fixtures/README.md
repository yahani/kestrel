# Test fixtures

Saved product pages so tests never hit live sites.

| File | Source |
|------|--------|
| `books_in_stock.html` | https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html |
| `books_sharp_objects.html` | https://books.toscrape.com/catalogue/sharp-objects_997/index.html |
| `scrapingdev_product_1.html` | https://web-scraping.dev/product/1 |
| `scrapingdev_product_2.html` | https://web-scraping.dev/product/2 |
| `mock_blue_mug.html` | Local mock shop (`python -m mock_site serve`), seed state, `/product/blue-mug` |
| `books_out_of_stock.html` | Synthetic: `books_in_stock.html` with stock set to "Out of stock" |
| `books_layout_changed.html` | Synthetic: `books_in_stock.html` with the price class renamed |

Fetched 2026-10-18. To refresh one (fetches politely, respects robots.txt):

```sh
uv run tracker test-selectors <url> -c config.example.yaml --save tests/fixtures/<name>.html
```
