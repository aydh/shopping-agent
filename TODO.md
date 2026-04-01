# TODO

Improvements and known issues, roughly ordered by impact/priority within each section.

---

## Bugs / Correctness

### Product hide/restore is not user-scoped `P1`
`is_hidden` lives on the `Product` table, which has no `user_id` column. When any user hides a product it disappears for all users, and when any user restores it, it reappears for everyone. The hide/restore API routes receive a `CurrentUser` but never use it to scope the operation. Fix: introduce a `user_product_preferences` table (or add `user_id` to a visibility join table) to record per-user hide state, keeping the shared `Product` row neutral.

### MCP OAuth consent page does not advance when consent was previously granted `P1`
When an MCP client re-authorises and Supabase has already stored the user's approval, it returns `auto_approved: true` along with a `redirect_url` in the initial `getAuthorizationDetails()` call. The consent page template correctly detects this and redirects without calling `approveAuthorization()` (which would return HTTP 400 on an already-approved request), but in practice the redirect stalls or the wrong URL is used, leaving the user on a blank consent page. Investigate the exact Supabase SDK response shape for the `auto_approved` path and ensure the redirect happens unconditionally. Also consider whether stale consent records in Supabase need to be cleared, and expose a way to do that from the Settings page.

### Shopping list predictions scoping — per-user visibility needed `P2`
`ConsumptionPrediction` rows already carry a `user_id`, but the logic that decides which products get predictions (and at what confidence threshold) is shared config, not per-user. There is no way for one user to exclude a product from predictions without globally hiding it (see above). Add per-user opt-out for both predictions and the "always include in list" / "never include" flags.

---

## Playwright / Browser Login

### Add interactive Playwright login for Woolworths `P1`
Woolworths login currently requires the user to manually export cookies from their browser using the Cookie-Editor extension. This is fragile — Woolworths tokens expire and the JWT refresh endpoint occasionally fails, requiring a fresh cookie import. Implement `login_interactive()` for `WoolworthsScraper` following the same Playwright pattern already used for Coles: open a persistent Chrome profile, navigate to the Woolworths login page, let the user complete sign-in (including 2FA if required), then harvest and persist the resulting cookies and JWT token. The `_bootstrap_akamai_cookies()` flow should still run beforehand to pre-seed bot-detection cookies.

### Harden and optimise the Coles Playwright login flow `P2`
The current Coles `login_with_credentials()` implementation has several rough edges: Incapsula bot-detection can reject requests if the delays between page interactions are too short or too uniform; the MFA completion flow (`complete_mfa()`) needs robust error handling for expired codes; and the persistent Chrome profile directory can become stale after browser updates. Improvements: add human-like randomised timing on top of the existing `PLAYWRIGHT_DELAY_*` constants, detect and handle Incapsula challenge pages, validate that saved cookies are still valid immediately after login completes, and add a clear error message when the profile directory is locked by another process.

### Get Playwright login working reliably in cloud / Render deployment `P2`
Playwright requires system browser dependencies that are not present in a standard Python container. The `render.yaml` build step needs to run `playwright install --with-deps chromium` (or equivalent), the `PLAYWRIGHT_CHANNEL` setting needs to point at a bundled browser, and `PLAYWRIGHT_HEADLESS` must be `true`. Document the required Render environment variables and confirm the Playwright install survives the build cache. Consider whether a separate login-helper container or a one-off login endpoint that streams browser output back to the user is a better model for cloud deployments where direct UI interaction is not possible.

---

## Performance

### Shopping list item operations open a redundant database session `P2`
Every mutation endpoint in `api_shopping_list/items.py` (quantity change, store change, delete) opens a second `async_session()` immediately after the first one commits, purely to fetch context for rendering the response HTML. This doubles the number of database round-trips for each user interaction and increases latency noticeably on hosted Supabase. Fix: reuse the already-open session to load the rendering context before it closes, passing the data through to the template render in a single transaction.

### Summary context rebuild is expensive on every item change `P2`
After each item mutation the route rebuilds the full shopping list summary context — re-fetching all items, resolving display names for both Coles and Woolworths variants of every product, and recomputing totals. For a list with 30–40 items this involves 60–80 product lookups on every quantity increment. Fix: compute totals and metadata incrementally from the delta (the changed item and its known prices) rather than re-querying the full list, and only reload display names for the affected item.

### `set_all_store` issues N individual UPDATE statements `P3`
`stores.py::set_all_store` iterates over every active item and updates `chosen_store` one row at a time inside a Python loop. Replace with a single bulk `UPDATE shopping_list_items SET chosen_store = :store WHERE shopping_list_id = :id AND is_removed = false`.

---

## Shopping List UI

### Summary cards are unclear about what each price represents `P2`
The three summary cards (Coles total, Woolworths total, Split total) display a headline price and several nested counts (available, unavailable, unmatched, matched & available), but the relationship between these numbers and the headline total is not obvious. A user seeing "Coles $47.20 · 3 unavailable" cannot tell whether the $47.20 includes estimated prices for unavailable items or excludes them entirely. Fix: add concise label text to each metric row, show an "(X items included)" sub-label under the total, and use a tooltip or info icon to explain what "unavailable" and "unmatched" mean in this context.

### Store choice per item is buried and unintuitive `P2`
The only way to change which store an item is bought from is a small store-selector control inside each item row, but it is not visually prominent and new users don't discover it. The "Split" card assigns stores automatically but provides no easy way to override individual choices afterwards. Improvements: make the chosen store more visually prominent (e.g. a clear Coles/Woolworths pill badge on the item row that is obviously clickable), add an inline "switch store" affordance that shows both prices side-by-side and lets the user tap to flip, and consider showing a summary of how many items are going to each store at the top of the list.

### Switching from a "Coles-first" to a "Woolworths-first" preferred shop `P2`
The "Submit to single store" flow (assign all items to one store and confirm) works, but there is no persistent concept of a preferred store. Each list generation defaults to cheapest-split, and switching to a Coles-only or Woolworths-only shop requires manually clicking "Assign all to Coles/Woolworths" every time. Add a per-user preferred store setting that pre-assigns items when a list is generated and makes it the default submit action on the confirm page.

### Filter and sort state is lost on page refresh `P3`
The current filter text and sort order are stored in `window._slFilter` / `window._slSort` and restored after HTMX swaps, but they are cleared on a full page load (e.g. browser refresh or navigating away and back). Persist them to `sessionStorage` keyed to the list ID so the user returns to the same view state.

---

## Navigation & Background Task State

### Navigating away from an in-progress order sync loses the UI progress `P2`
Order sync runs as a browser SSE connection. If the user navigates to another page mid-sync, the SSE stream is torn down and progress is lost — though the database writes continue server-side (since `sync_orders` runs inside the route handler, not a background task). When the user returns to the Orders page there is no indication of whether the sync finished, partially completed, or is still running. Fix: move sync execution into a proper background task (e.g. `BackgroundTasks` or APScheduler), persist a lightweight sync-status record to the DB (store, started_at, finished_at, orders_added, status: running/done/error), and show a live status banner on the Orders page that polls this record so progress is visible regardless of navigation.

### Background price refresh has no visible status history `P2`
The scheduler runs price refreshes every 4 hours but there is no way to tell from the UI when the last refresh ran, how many products were updated, or whether any failed. The `/api/prices/refresh-progress/{store}` endpoint only provides in-flight progress for a manually-triggered refresh. Add a `price_refresh_log` table (or a simple JSON record in app state) that records: store, started_at, finished_at, products_attempted, products_updated, error_count. Surface this on the Prices page and Settings page as "Last refreshed: 2 hours ago · 412/415 updated".

---

## Search & Navigation UX

### Search terms are cleared on navigation and page refresh `P3`
Search inputs on the Prices page (product search, match search), the Orders page, and the Shopping List page all reset to empty when the user navigates away or refreshes. Since these pages are frequently revisited during a shopping session this is disruptive. Persist search input values to `sessionStorage` per-page and restore them on load, including after HTMX partial swaps.

---

## Dynamic Updates

### Tables do not update automatically when background operations complete `P2`
When a price refresh or order sync completes in the background, the corresponding tables (Prices page product list, Orders page) don't reflect the new data until the user manually refreshes. Add lightweight HTMX polling or a push mechanism (SSE keep-alive with refresh events) on these pages so rows update within a few seconds of changes being committed to the database. This is especially important for the scheduled price refresh — a user opening the Prices page mid-refresh should see prices populating in real time.

---

## MCP Tools

### MCP tools contain bugs and have had limited testing `P1`
The MCP server (`routes/mcp.py`) exposes 19 tools but most have had minimal real-world usage. Known issues include: `sync_orders` fetches all orders before upserting (losing streaming behaviour), `refresh_prices` does not pass the user's scraper instance correctly (defaults to global singleton and ignores per-user cookies), `find_product_match` can return a match that belongs to a different user's product set, and several tools do not return useful error messages when the user has no active shopping list. Conduct a systematic review of all 19 tools, test each against a real Supabase + store-cookie setup, and fix the scraper-scoping and error-handling issues.

### MCP tool descriptions and parameter docs are incomplete `P3`
Some tools have minimal docstrings and no examples in their parameter descriptions. LLM agents (including Claude) perform better when tool descriptions include the expected call sequence, what pre-conditions must be met (e.g. "requires cookies to be imported first"), and what the response fields mean. Improve all 19 tool docstrings with concrete examples and pre/post-condition notes.

---

## Code Quality & Maintainability

### Extract reusable UI components from inline template code `P3`
Several patterns are duplicated across Jinja2 templates: store-branded price badges (red/green), product image with fallback placeholder, the quantity stepper widget, HTMX loading indicator overlays, and the page-header / breadcrumb layout. Extract these into Jinja2 macros (e.g. `macros/product.html`, `macros/forms.html`) and replace all inline duplications. This will make future style changes consistent and reduce template file sizes.

### Consolidate repeated session + RLS boilerplate in route handlers `P3`
Many route handlers open an `async_session()` block, call `set_rls_claims()`, perform a query, close the session, then open a second session to render the response. This pattern appears in at least a dozen places across the route sub-packages. Introduce a FastAPI dependency (or a context-manager helper) that handles the session + RLS claim setup and yields it for the full request lifetime, eliminating the dual-session pattern and reducing boilerplate.

### Refactor price comparison rendering into a shared helper `P3`
The Prices page, Shopping List confirm page, and several HTMX partials each build a "Coles price vs Woolworths price" display using slightly different template logic. Consolidate into a single Jinja2 macro that accepts a `PriceComparison` object and renders the branded comparison, so visual updates only need to be made in one place.

### Review and tighten error handling in scraper methods `P3`
Scraper methods (`get_product_price`, `add_to_cart`, `search_product`) catch broad `Exception` and log a warning, which makes it hard to distinguish network timeouts, authentication failures, rate-limit responses, and API contract changes. Introduce a `ScraperError` hierarchy (`AuthenticationError`, `RateLimitError`, `NotFoundError`) so callers can handle each case appropriately — e.g. surfacing an "authentication expired" message to the user rather than a generic failure.

---

## API Design

### Redesign routes to be resource-centric `P2`
The API URLs are action-oriented rather than resource-oriented, which makes the surface area harder to understand, document, and extend. Examples of the current pattern: `POST /api/shopping-list/generate`, `POST /api/shopping-list/confirm/{id}`, `POST /api/shopping-list/close/{id}`, `POST /api/prices/match-products`, `POST /api/prices/confirm-match/{id}`, `POST /api/shopping-list/submit-split`. A resource-centric design would model state transitions as `PATCH` on the resource with a `status` or `action` field (e.g. `PATCH /api/shopping-lists/{id}` with `{"status": "confirmed"}`), use `POST` only to create a resource, and use `DELETE` only to destroy one. The shopping list sub-router alone has `new`, `generate`, `add-predictions`, `confirm`, `close`, `set-store`, `submit-store`, `submit-split` as separate POST endpoints where most are transitions on the same list resource. A full redesign would also consolidate the prices sub-router (`match-products`, `confirm-match`, `manual-match`, `search-match`, `search-match/confirm`) into a consistent `/api/product-matches` resource. Note: this is a breaking change for any MCP tool or external caller, so the MCP tool surface should be reviewed and updated in the same pass.

---

## Logging

### Fix noisy / incorrect log entries when fetching Supabase JWKS `P2`
In `auth.py`, the RS256/ES256 verification path fetches JWKS from Supabase, then falls back to `/auth/v1/user` if local verification fails. The fallback is triggered by a bare `except Exception:` (line 125) with only a `debug`-level log — which means any misconfiguration, key ID mismatch, or transient network error silently swallows the root cause and issues a synchronous HTTP call to Supabase on every non-cached request. In practice, if the JWKS keys endpoint is accessible but the `kid` in the token doesn't match any key in the response (a common misconfiguration), the code raises a 401 `"No matching JWT key found"` that is immediately caught and retried via `/auth/v1/user`, with nothing in the logs to explain why. Fix: distinguish between "JWKS endpoint unavailable" (acceptable fallback) and "JWKS returned keys but none matched" (likely a configuration error that should log at `WARNING` with the mismatched kid). Also make the fallback path log at `WARNING` rather than `DEBUG` when it is invoked so auth issues are visible in the INFO-level console output.

### Standardise logging levels and context across the codebase `P3`
Log coverage is uneven: services and scrapers have loggers and use them reasonably, but most route handlers (13 `logger.` calls across all of `routes/`) emit nothing — success, failure, and unexpected conditions are all silent from the route layer. The MCP tools use an ad-hoc `[MCP]` prefix string rather than a child logger. Some places log at `WARNING` for expected recoverable conditions, others use `DEBUG` for genuine errors. Define a logging convention: `DEBUG` for per-item trace data (individual price fetches, single row upserts), `INFO` for operation boundaries (sync started/finished, list confirmed), `WARNING` for recoverable external failures (store API returned unexpected status, JWKS fetch failed), `ERROR` for unexpected internal failures. Apply consistently across routes, services, and scrapers, and replace `[MCP]` string prefixes with a `logging.getLogger("shopping_agent.mcp")` child logger.

---

## Testing

### Establish integration tests against a real (test) database `P1`
All existing tests mock the SQLAlchemy session with hand-rolled `FakeResult` / `FakeSession` objects. This means database constraints, ORM relationship loading, RLS behaviour, and multi-step transactions are never exercised. A single `pytest` fixture that spins up a local PostgreSQL instance (via `pytest-docker` or a pre-existing test DB URL from env) and runs `alembic upgrade head` would unlock proper integration testing for services and route handlers. The `conftest.py` already has good patterns for fixtures; extending it with a real async session fixture scoped to each test function (with rollback teardown) would let existing service tests run against actual SQL with minimal changes.

### Route handlers have no HTTP-level tests `P2`
`test_api_routes.py` calls route handler functions directly with mock sessions — it does not use FastAPI's `TestClient` or `AsyncClient`. This means request parsing, dependency injection, response status codes, headers, and redirect behaviour are never tested. Add a test module using `httpx.AsyncClient(app=app, base_url="http://test")` to exercise at least the most critical paths end-to-end: cookie import → validate, order sync SSE output, list generate → confirm → cart stream, and the auth redirect behaviour (unauthenticated requests redirecting to `/login`).

### No tests for scrapers, auth, or MCP tools `P2`
Three significant areas have zero test coverage: (1) `ColesScraper` and `WoolworthsScraper` — even the non-network parts (response parsing, cookie normalisation, JWT extraction) could be unit tested against fixture JSON; (2) `auth.py` — the JWKS/fallback logic, token caching, and cookie-vs-Bearer dispatch are untested despite being security-critical; (3) `routes/mcp.py` — none of the 19 MCP tools have tests. Start with the easiest wins: scraper response-parsing methods with recorded API response fixtures, auth token decode with known HS256 test tokens, and a handful of MCP tools using mocked sessions.

### Measure and track test coverage `P3`
There is no coverage configuration in `pyproject.toml` and no CI enforcement of a minimum threshold. Add `pytest-cov` to dev dependencies, configure `[tool.pytest.ini_options]` with `--cov=shopping_agent --cov-report=term-missing`, and establish a baseline. Even knowing the current coverage percentage helps prioritise where new tests will have the most impact.
