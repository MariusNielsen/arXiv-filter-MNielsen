# arXiv filter

Daily filtered digest of arXiv (math.AT, AG, CT, KT, RT) and hexagonmath.org,
by followed authors and keywords. Runs on GitHub Actions around 06:15 Berlin (with backup runs until ~11:40) and
publishes to GitHub Pages:

- **Digest:** `https://mariusnielsen.github.io/arXiv-filter-MNielsen/`
- **Atom feed:** `…/feed.xml` (last 21 days of matches)
- **Archive:** `…/archive/`

Sections: followed authors → keyword in title → keyword in abstract → every other
math.AT paper (incl. cross-lists) → collapsed "rest" of math.KT. Each paper shows why
it matched. The backtest additionally lists unmatched papers of all categories.

## Files you edit

| File | What |
|---|---|
| `authors.yaml` | `always` / `watch` author lists, with name variants |
| `keywords.yaml` | `strong` patterns (title or abstract), `title_only` patterns |
| `config.yaml` | categories, always-shown categories (math.AT), "rest" categories, RT restrictions, your papers |

Editing a file on github.com (works from the phone too) takes effect at the next run.

## Manual runs (Actions → "daily filter" → Run workflow)

- `mode: backtest`, `days: 90`: classifies the last 90 days into `docs/backtest.html`
  without touching state. Use it to tune the lists.
- `mode: citers`: writes `citers_suggested.yaml`, authors citing your papers who aren't
  listed yet (also runs monthly).

## Matching rules

- Authors match on full name (first given name + surname), so "Achim Krause" does not
  match Henning Krause. Add `variants:` for alternative spellings.
- `watch` authors only count inside your categories or together with a keyword.
- Replacements (`replace`, `replace-cross`) are shown only for followed authors.
- A paper whose only subscribed category is math.RT needs an author or title-keyword hit.
- Hexagon works carry their authorship label (human / mixed / AI-generated); set
  `hexagon.drop_ai_generated: true` in `config.yaml` to hide AI-generated ones.

Local: `pip install pyyaml && python arxiv_filter.py`.
