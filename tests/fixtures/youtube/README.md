# YouTube Data API test fixtures

* `error_invalid_key.json`, `error_missing_key.json` were captured from the live API
  (`videoCategories.list` with a fake key / no key). They contain no secrets.
* All other files were constructed by hand to match the documented YouTube Data API v3
  response formats. They are deterministic and never fetched at test time.

Notable edge cases encoded here: HTML-escaped snippet text in `search.list`, hidden like
counts / disabled comments (keys absent -> must be `None`, never 0), `P0D` durations,
hidden subscriber counts, fractional-second timestamps, and pagination tokens.
