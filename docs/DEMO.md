# 2-minute demo script

For the portfolio video: no voice needed, captions on screen. Before recording:
- Run `uv run python -m mock_site reset`.
- Delete `mock_site/mock-demo.db` if it exists.
- Have three windows ready: two terminals (font size up) and the email inbox, or the
  `--dry-run` output if you're not sending email.

| Time | On screen | Caption |
|---|---|---|
| 0:00 | The README's digest screenshot | "Small online stores check competitor prices by hand. This does it daily." |
| 0:08 | Terminal 1: `uv run python -m mock_site serve` | "A mock competitor shop, so the demo can change prices on demand." |
| 0:15 | Browser: `http://127.0.0.1:8000/product/blue-mug` | "Blue Mug: $24.00, in stock." |
| 0:22 | Terminal 2: `uv run tracker run -c mock_site/tracker-config.yaml` | "Day 1: the tracker records every price. Polite: robots.txt, 5 s between requests." |
| 0:45 | Browser: `http://127.0.0.1:8000/admin`, set Blue Mug to 19.50 and Red Mug's stock to 0 | "The competitor cuts a price and runs out of stock." |
| 0:58 | Terminal 2: `uv run tracker run -c mock_site/tracker-config.yaml --dry-run` | "Day 2: one digest with exactly what changed, including that they now undercut you." |
| 1:20 | The digest preview (or the email on a phone) | "Delivered by email or Slack, grouped by what matters." |
| 1:35 | Google Sheet, Latest tab | "The full history lands in a Google Sheet, too." |
| 1:45 | GitHub: Actions → Daily run, then the green run | "Runs free every day on GitHub Actions." |
| 1:55 | The README | "Setup from $150. Link in the description." |

Notes:
- For a real email in the video, use `mock_site/demo-email.yaml`: copy
  `mock_site/tracker-config.yaml`, then set `alerts.email.enabled: true` and `to`. Your
  `.env` needs SMTP set up first ([SETUP.md](SETUP.md)). The file is gitignored.
- Each run takes about 20 s because of the 5 s politeness delay. Speed it up in editing,
  not in the config.
