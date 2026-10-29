# Evodex price feed

The static daily price and catalog feed for **Evodex**, an unofficial iOS app for Pokémon TCG
collectors. Once a day a GitHub Actions job joins the card catalog from TCGdex with TCGplayer
prices from TCGCSV and publishes plain JSON files to GitHub Pages:

**https://ueski.github.io/evodex-feed/** (start at [`manifest.json`](https://ueski.github.io/evodex-feed/manifest.json))

The app only ever downloads these files. It never calls TCGCSV, whose rules ask that user-facing
apps don't; a server-side daily job like this one is the intended pattern. This repository holds
only what that job needs: the data pipeline (`pipeline/`), the workflow and this README. There is
no app code here.

> **Not an API.** The feed exists for the Evodex app. It has no uptime promise and may change
> or disappear. For your own project, use [TCGCSV](https://tcgcsv.com) and
> [TCGdex](https://tcgdex.dev) directly and follow their rules.

## Data sources and attribution

| What | Source | Notes |
|---|---|---|
| Prices (market, low, mid, high, direct low; USD) | **TCGplayer**, via [TCGCSV](https://tcgcsv.com) | TCGCSV mirrors TCGplayer's catalog and prices once a day. The prices are TCGplayer's data. TCGplayer is credited for **prices only**: no TCGplayer images are used. Thank you, TCGCSV. |
| Cards, sets, numbers, rarities, image URLs | [TCGdex](https://tcgdex.dev) (`api.tcgdex.net`) | Open-source card database. Thank you, TCGdex. |
| Fallback image URLs (cards TCGdex has no image for) | A TCGdex scan of the same card listed twice by TCGdex, else [pokemontcg.io](https://pokemontcg.io) / Scrydex **only** (`images.pokemontcg.io`, `images.scrydex.com`) | URLs only; no images are stored here. A card is matched to pokemontcg.io by set, number and name; when pokemontcg.io's file is missing, Scrydex's file of the same card id is used after a check that it is not Scrydex's card-back placeholder. Cards without a verified source get none. |
| National Dex species per card (`dx`) | [PokéAPI](https://pokeapi.co) static CSV export | Read at most every 30 days from GitHub, never from the REST API. |

**Notice.** Pokémon and all card names, card text, set names, artwork and images are © Nintendo,
Creatures, GAME FREAK and The Pokémon Company, or their other respective owners. Prices are
TCGplayer's. Evodex and this feed are **unofficial and fan-made**; they are not affiliated with,
endorsed, sponsored or approved by Nintendo, The Pokémon Company, Creatures, GAME FREAK,
TCGplayer, TCGCSV or TCGdex. The [MIT license](LICENSE) covers this repository's **code only**,
not the data the code downloads or publishes.

## Schedule

| Step | UTC | Türkiye | New York (EST) |
|---|---|---|---|
| TCGCSV publishes the day's TCGplayer dump | ~20:05 | ~23:05 | ~15:05 |
| This workflow starts (`cron: 15 21 * * *`) | 21:15 | 00:15 (next day) | 16:15 |
| New feed live (warm run: pipeline + build + deploy) | ~21:25 | ~00:25 | ~16:25 |
| If TCGCSV is late: `last-updated.txt` is polled every 15 min | until 23:15 | until 02:15 | until 18:15 |

The job runs once per TCGCSV build. If TCGCSV still serves the build the feed already has, it does
nothing; on a scheduled run it first waits up to 2 hours for today's build. GitHub may start
scheduled runs late under load. It can be started by hand at any time (`workflow_dispatch`).

## How a run works

```
21:15 UTC  scripts/state.sh pull state     data branch → state/ (manifest, index, history/, daily/)
           publish_feed.py check           TCGCSV last-updated.txt vs state/manifest.json; same → stop
           run.py                          TCGdex + TCGCSV → pipeline/out/bundle/{index,prices,prices-market,manifest}.json
           publish_feed.py build           fold today (and pipeline/history-seed/) into state/ → site/ (complete site)
           validate_feed.py site           sha256, counts, sizes, guards; any problem → stop
           scripts/state.sh push site      site/{manifest,index,history,daily} → next commit on the data branch
           upload-pages-artifact + deploy-pages   atomic replacement of the whole site
```

Every step must succeed before the next one runs. **If the pipeline, the build or the validation
fails, nothing is pushed or deployed and the last good feed stays live** (Pages deployments are
atomic). The next run, or a manual one, tries again.

### Guards (a bad day never reaches the app)

`publish_feed.py build` enforces the guards against the previous feed before it writes anything;
`validate_feed.py` checks all of them again, plus structure and history, on the exact bytes that
will be uploaded. Either one failing stops the run:

| Guard | Why |
|---|---|
| `sha256` and byte size of `index.json`, `prices.json`, `prices-market.json` match `manifest.json` | The app verifies the same hash; a mismatch would be rejected on every device. |
| `prices.json` lists **≥ 90 % of the previous feed's products** | Mirrors the app's own rule (it keeps its old prices below 90 %). Catches a partial TCGCSV outage. |
| Catalog keeps ≥ 95 % of its cards; `prices.json` keeps ≥ 80 % of its bytes | Partial upstream outage or partial pipeline run. |
| **Cards are only ever added:** every card of the previous feed's `index.json` and of `pipeline/catalog-floor.json` (every catalog an app version ships with) is still in `index.json`. `allow_shrink` does **not** override this; only `pipeline/overrides.json` `"removedCards"` does | The app replaces its bundled catalog with this `index.json`; a missing card shows "Card not found" for every collection entry that references it. On 2026-10-28 TCGdex served an older set list and 34 of 21,290 cards (0.16 %) went missing, well inside the 95 % guard. |
| TCGCSV build not older than the previous feed's | Never go back in time. |
| History carried over: no fewer days, no lost daily snapshots, no lost history files | A run that lost its state would otherwise publish a feed with its history wiped. |
| First run: ≥ 15,000 cards and ≥ 15,000 products, one day of data plus history seeds only | There is nothing to compare the first day with. |
| Fallback images (`im` in `index.json`) only on `images.pokemontcg.io` / `images.scrydex.com`, custom TCGdex image bases (`i`) only on `assets.tcgdex.net`; any `tcgplayer-cdn.tcgplayer.com` URL fails the run | TCGplayer images are not used (IP decision); TCGplayer is a price source only. |
| `daily/` and `history/` match the manifest exactly; every series as long as its dates; no file > 100 MB, site < 1 GB | Structural integrity and GitHub Pages limits. |

After checking what changed, a shrink can be accepted once with **Run workflow → allow_shrink**. A card that
disappears cannot: if TCGdex really removed or renamed it, list its id in `pipeline/overrides.json`
`"removedCards"` with the reason (change it in the app repository's `tools/pipeline/` and copy it here, see
"Repository layout").

## History and state

The feed is stateful: `history/<setId>.json` gains one column per day and `daily/` keeps the last
90 snapshots. That state is kept on this repository's **`data` branch** (only `manifest.json`,
`index.json`, `history/` and `daily/`; `index.json` since 2026-10-29, for the "cards are only ever added"
guard). Each run reads the branch, folds today in, validates, commits the new
state on top of the branch (fast-forward only) and then deploys the site.

Why a branch, not the live site, as the source of truth:

* **Survives the host.** If GitHub Pages is disabled, re-created, moved to a custom domain or has
  an incident that answers 404, the history is still in git. Reading state back from the site
  would treat such a 404 as "first run" and could publish a feed with a year of history wiped.
* **Unambiguous first run.** `git ls-remote --exit-code` tells "branch absent" (bootstrap) apart
  from "network error" (abort).
* **Auditable and reversible.** Every day is a commit; a bad day can be reverted.
* **Recovery.** With no `data` branch but a live feed, the run reads the state back from the site
  (`--previous <feed URL>`) and pushes it as the new branch.

Cost: the job needs `contents: write`, and the branch grows by the day's delta (see "Data branch size"). The
publish order (validate → push state → deploy) means a failed deploy leaves the state one day
ahead of the site, which the next run repairs; it can never lose a day of history.

**First run.** No `data` branch and no live `manifest.json` (404): the run bootstraps. History
starts on that day; TCGCSV's price archive is offline, so earlier days cannot be back-filled from it.

**History seeds.** `pipeline/history-seed/<YYYY-MM-DD>.json` is a `prices-market.json` of an earlier TCGCSV
build that we downloaded ourselves. Every build folds it into each history file that lacks that date (at its
sorted position; a file that has the date keeps its own values) and into `daily/` while the date is within the 90
newest. Days in between are not invented: the history has 2026-10-21, then 2026-10-28, and the app draws that gap
dashed. Once the date is on the `data` branch the seed is a no-op. `2026-10-21.json` is the snapshot behind the
app's bundled prices (TCGCSV build `2026-10-21T20:05:22+0000`), so the charts start a week before the first
nightly run.

### Data branch size

`index.json` adds about 0.5 MB once (zlib), then small deltas: it only changes when the catalog or the product
mapping does (it has no timestamps). For the rest,
measured locally with 32 simulated days on real data and deliberately heavy price churn (each price
moves on 35 % of days): the branch packed to **10 MB after `git gc`** (about 0.3 MB per day; about
1.3 MB per day before the server repacks). Expect roughly 100–150 MB per year, well under GitHub's
1 GB recommendation. Each run fetches only the newest commit (`--depth=1`): 1.5 MB on day 1,
about 20 MB compressed at full retention. If the repository size ever matters, squash the branch
(see "Operations"); the price history in the files is not affected, only old commits go.

## Run it locally

Python 3.9+ standard library only; no packages to install. These are the workflow's commands:

```bash
(cd pipeline && python3 -m unittest)                            # tests

PREVIOUS=$(scripts/state.sh pull state https://ueski.github.io/evodex-feed/)   # "state", the URL, or "" (first run)
python3 pipeline/publish_feed.py check --previous "$PREVIOUS"   # changed=true|false
python3 pipeline/run.py --workers 4                             # cold: ~35 min; warm: a few minutes
python3 pipeline/publish_feed.py build --bundle pipeline/out/bundle --out site --previous "$PREVIOUS"
python3 pipeline/validate_feed.py site --previous "$PREVIOUS"   # exit 1 = don't deploy
# scripts/state.sh push site "message"                          # CI only: commits + pushes the data branch

cd site && python3 -m http.server 8000                          # app: launch argument -FeedBaseURL http://localhost:8000/
```

Without a `data` branch on `origin` (a fresh clone of a fork), `pull` falls back to the live site
or to a bootstrap. `--offline` runs the pipeline from `pipeline/cache/` only. `pipeline/cache/`,
`pipeline/out/`, `state/` and `site/` are git-ignored.

## Feed layout

All files are minified UTF-8 JSON. GitHub Pages compresses them in transit (`gzip`), and each
response carries an `ETag`. Sizes are from the local run of 2026-10-28 (TCGCSV build
`2026-10-28T20:06:05+0000`):

| Path | What | Raw | gzip -6 (≈ as served) |
|---|---|---:|---:|
| `manifest.json` | The only file the app polls (`If-None-Match`) | 757 B | 424 B |
| `index.json` | Catalog: 21,290 cards, 205 sets | 2.9 MB | 476 KB |
| `prices.json` | Latest prices, 5 fields: 22,305 products, 36,130 rows | 1.1 MB | 338 KB |
| `prices-market.json` | Latest prices, market only | 505 KB | 128 KB |
| `history/<setId>.json` | 200 files, one per set with priced cards | day 1: 610 KB, day 2: 744 KB (all files) | 185 KB, 247 KB |
| `daily/<YYYY-MM-DD>.json` | One per price date, last 90 kept | 505 KB each | 128 KB each |
| `.nojekyll` | Empty | 0 | 0 |
| **Whole site, first day** | 206 files | **5.4 MB** | **1.2 MB** |
| Whole site, full retention (projected) | 365 history dates, 90 daily files | ~100 MB | ~20 MB |

The projection uses the app repository's simulation of 365 days of price movement (history ≈ 48 MB
raw / 7.5 MB gzip); GitHub Pages allows 1 GB per site. A card screen downloads one history file:
tens of KB after a year.

### manifest.json (the only file the app polls)

```json
{"v": 1,
 "generatedAt": "2026-10-28T21:17:58Z",
 "tcgcsvBuild": "2026-10-28T20:06:05+0000",
 "date": "2026-10-28",
 "index": {"path": "index.json", "sha256": "43ca50…", "bytes": 2902783, "cards": 21290, "sets": 205},
 "prices": {"path": "prices.json", "sha256": "776633…", "bytes": 1121166, "products": 22305, "rows": 36130},
 "pricesMarket": {"path": "prices-market.json", "sha256": "aa80f9…", "bytes": 516969},
 "history": {"pathTemplate": "history/{setId}.json", "from": "2026-10-28", "to": "2026-10-28", "days": 1, "retentionDays": 365, "sets": 200},
 "daily": {"pathTemplate": "daily/{date}.json", "dates": ["2026-10-28"], "retentionDays": 90}}
```

* `date` = `tcgcsvBuild[:10]`, the price date. `generatedAt` = when the pipeline ran.
* `sha256` / `bytes` are over the exact, uncompressed bytes of each file.
* `prices.products` / `prices.rows`: products and (product, printing) rows in `prices.json`.
* `history.days` = most dates in any history file; `history.sets` = number of history files. A
  set with no priced product has no file (404 = "no history yet").
* `daily.dates` is ascending and lists exactly the files present.
* `links` (optional, Evodex 0.3.1+): `{"tcgplayerAffiliate": "<template>"}` switches the app's TCGplayer
  affiliate links on; absent = off. See "Affiliate links".
* Adding keys keeps `v: 1`; a breaking change bumps `v`, and apps ignore a `v` they don't know.

### prices.json / prices-market.json

```json
{"v": 1, "tcgcsvBuild": "2026-10-28T20:06:05+0000",
 "subTypes": ["Normal", "Holofoil", "Reverse Holofoil", "1st Edition", "1st Edition Holofoil", "Unlimited", "Unlimited Holofoil"],
 "p": {"42382": [[1, 95465, 89999, 105000, 250000, 99500]]}}
```

Rows are `[subTypeIdx, market, low, mid, high, directLow]` in integer US cents (`null` = none);
`prices-market.json` keeps `[subTypeIdx, market]`. `subTypes` is each file's own table; join with
`index.json` by name. Keys are TCGplayer product ids.

### index.json

The card catalog (sets, cards, numbers, rarities, TCGplayer product ids and printings, image
URLs, species), in the compact format documented in [`pipeline/bundle.py`](pipeline/bundle.py). Images: a card
without `i` uses TCGdex's standard path; `"i": "<url>"` is another TCGdex image base (a TCGdex twin's scan);
`"i": 0` plus `"im"` is a complete pokemontcg.io / Scrydex file; `"i": 0` alone means no image. The catalog only
grows (see "Guards").

### history/&lt;setId&gt;.json

```json
{"v": 1, "setId": "base1", "dates": ["2026-10-28", "2026-10-29"],
 "subTypes": ["Normal", "Holofoil", "Reverse Holofoil", "1st Edition", "1st Edition Holofoil", "Unlimited", "Unlimited Holofoil"],
 "s": {"42382.1": [95465, 96000], "106999.6": [217224, null]}}
```

One file per TCGdex set. Series key `"<productId>.<subTypeIdx>"` (index into this file's
`subTypes`); one market price in cents (or `null`) per date. Re-running a date replaces its column;
the last 365 dates are kept; all-null series are dropped. `dates` lists price dates only: a day without a build
(or before the first run, apart from history seeds) is absent, never interpolated.

### daily/&lt;YYYY-MM-DD&gt;.json

That day's `prices-market.json`, unchanged, for the last 90 price dates.

## Etiquette

* **TCGCSV:** identifiable User-Agent with this repository's URL, sequential requests 150 ms apart,
  one full sync per daily build (gated by `last-updated.txt`), a few hundred requests a day, well
  under the 10k/day limit. The app never contacts TCGCSV.
* **TCGdex:** 4 workers, at most ~13 requests/s, set lists cached 12 hours (so every nightly run reads
  today's lists) and cards 14 days.
* **pokemontcg.io:** at most 30 requests/minute, 7-day cache, only sets that need a fallback image.
  **Image CDNs** (pokemontcg.io / Scrydex): one HEAD per new fallback URL, ≤ 10/s per host, results cached;
  plus one HEAD of an id Scrydex cannot have, per run that checks Scrydex URLs (its placeholder's ETag).
* The HTTP cache (`pipeline/cache/`) is kept between runs with `actions/cache`; losing it only
  makes the next run cold (~35 min, ~22k TCGdex requests).

## Affiliate links

Evodex 0.3.1+ can wrap its "View on TCGplayer" links in a TCGplayer affiliate link (TCGplayer's
program runs on Impact) and then shows "Evodex may earn a commission from TCGplayer purchases."
under every such link. It is **off**: `pipeline/links.json` holds `"tcgplayerAffiliate": null`, so
the manifest has no `links` block. The switch lives only in this repository (the app repository's
`feed_sync.py copy` never touches it).

To switch it on after the affiliate application is approved:

1. Put the Impact tracking link for TCGplayer into `pipeline/links.json`, with `{url}` where the
   destination goes (Impact's deep-link parameter is usually `u`):
   `"tcgplayerAffiliate": "https://tcgplayer.pxf.io/c/<id>/<ad>/<campaign>?u={url}"`
2. Commit to `main`, then `gh workflow run price-feed.yml -f force=true` (or wait for the next
   nightly build). The manifest gets `"links":{"tcgplayerAffiliate":"…"}`; apps pick it up at
   their next feed check and keep the last value while offline.
3. Update the **Notice** above: Evodex is then a TCGplayer affiliate, so drop TCGplayer from
   "not affiliated with" (the app's own disclaimers switch by themselves).

Rules (checked by `publish_feed.py build` and `validate_feed.py`, and again by the app): `https`,
`{url}` exactly once, host `tcgplayer.pxf.io`, `www.tcgplayer.com` or `tcgplayer.com`, no user info
or port, at most 1,024 characters. An invalid value stops the build, so the live feed is never
changed by a typo. The app accepts nothing else either, so even a tampered feed cannot send users
to another site. To switch off: `null` (no `links` block) or `""` (an explicit "off" that also
overrides a template built into an app version), then rebuild as in step 2.

## Operations

| Task | How |
|---|---|
| Turn the feed off / on | `gh workflow disable "Price feed"` / `gh workflow enable "Price feed"` |
| Switch affiliate links on / off | `pipeline/links.json` `tcgplayerAffiliate`, then `gh workflow run price-feed.yml -f force=true` ("Affiliate links") |
| Run now | `gh workflow run price-feed.yml` (add `-f force=true` to rebuild the same TCGCSV build) |
| Accept a sharp shrink once | `gh workflow run price-feed.yml -f allow_shrink=true` |
| Undo a bad day | Revert the day's commit on `data` (`git revert`, push), then `gh workflow run price-feed.yml -f force=true` |
| Fix a set mapping or card name | Edit `tools/pipeline/overrides.json` in the app repository (format in `setmap.py` / `run.py`), run `run.py --offline` and the tests there, then `python3 tools/pipeline/feed_sync.py copy <this checkout>` and commit here to `main` |
| Remove a card on purpose | Add `"<cardId>": "<reason>"` to `overrides.json` `"removedCards"` (same route as above); without it the run fails |
| Add a released app catalog to the floor | In the app repository: `python3 tools/pipeline/publish_feed.py floor --add Binderly/Resources/Data/index.json --label "Evodex X.Y.Z bundle"`, then copy as above |
| Protect the state | Settings → Rules → Rulesets → new branch ruleset for `data`: block deletion and force pushes (the job only fast-forwards) |
| Shrink the `data` branch | Rarely needed (see "Data branch size"). Squash it into one commit: `git fetch origin data && git checkout --orphan squash origin/data && git commit -m "Squash state" && git push -f origin squash:data` (lift the force-push rule for that push) |
| Get told about failures | GitHub e-mails failed scheduled runs to the person who last changed the workflow's `cron`; check Actions → Price feed |

GitHub disables scheduled workflows after 60 days without repository activity; the last step of
each scheduled run re-enables the workflow to keep the schedule alive.

## Repository layout

| Path | What |
|---|---|
| `.github/workflows/price-feed.yml` | The daily job (schedule, concurrency, state, build, validate, deploy) |
| `scripts/state.sh` | Reads and writes the `data` branch |
| `pipeline/run.py` | Pipeline orchestrator: TCGdex + TCGCSV → `out/bundle/` |
| `pipeline/publish_feed.py` | `check` (new TCGCSV build?) and `build` (fold today into the previous feed) |
| `pipeline/validate_feed.py` | The final gate before deploy |
| `pipeline/history.py` | History file format and the daily fold (`append_column`) |
| `pipeline/sources.py`, `net.py` | Fetchers, polite HTTP client and on-disk cache |
| `pipeline/setmap.py`, `matching.py`, `normalize.py`, `variants.py` | TCGdex set ↔ TCGplayer group mapping and card ↔ product matching |
| `pipeline/images.py`, `species.py`, `bundle.py` | Fallback images, species, compact JSON encoders |
| `pipeline/overrides.json` | Hand-maintained mapping fixes; `removedCards` (the only cards allowed to leave the catalog) |
| `pipeline/catalog-floor.json` | Every card a shipped app catalog contains; `index.json` must always contain them |
| `pipeline/history-seed/` | One-time history seeds (feed-only; see "History and state") |
| `pipeline/links.json` | The affiliate-link switch (feed-only; see "Affiliate links") |
| `pipeline/tests/` | `python3 -m unittest` |

Everything in `pipeline/` except `history-seed/` and `links.json` is a byte-identical copy of the shared files in the Evodex app
repository's `tools/pipeline/`, which is the source of truth (its `feed_sync.py check` / `copy` lists them). Make
pipeline changes there and copy them here, so the bundled catalog and the nightly one come from the same code.

## License

Code: [MIT](LICENSE). Data: not covered; see "Data sources and attribution" above.
