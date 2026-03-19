# APIS.md

API reference for the shopping agent. All routes are registered on the FastAPI app at `http://localhost:8000`.

---

## Design Notes

- **HTML-first responses**: Most endpoints return HTML fragments for HTMX consumption, not JSON. Exceptions are batch chart endpoints and a few redirects.
- **SSE streaming**: Order sync and cart-add are Server-Sent Events streams — connect and consume events until the stream closes.
- **Two stores**: Every store-scoped endpoint accepts `store` as either `"coles"` or `"woolworths"`.
- **View routes**: `GET /`, `/orders`, `/predictions`, `/prices`, `/shopping-list`, `/product-lookup`, `/settings`, `/confirm` render full HTML pages.

---

## Authentication

> Manage stored session cookies per store. The scrapers use httpx with these cookies — there is no username/password flow.

| Method | Path | Body | Description |
|--------|------|------|-------------|
| `POST` | `/api/auth/import-cookies/{store}` | JSON array of cookie objects | Import cookies from browser DevTools or Cookie-Editor extension |
| `GET` | `/api/auth/validate/{store}` | — | Test stored cookies against the live store API |
| `POST` | `/api/auth/logout/{store}` | — | Clear stored cookies for a store |

**Typical flow:** Export cookies from your browser → `POST /api/auth/import-cookies/coles` → `GET /api/auth/validate/coles` to confirm they work.

---

## Orders

> Sync purchase history from Coles and Woolworths into the local database.

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `GET` | `/api/orders/sync-stream/{store}` | — | SSE stream; emits one HTML row per order as it's saved |
| `GET` | `/api/orders/{order_id}/items` | `order_id` (path) | HTML table of items for a specific order |
| `DELETE` | `/api/orders/purge/{store}` | — | Delete all orders and price history for a store |

**SSE events** from `/sync-stream/{store}`: each event is an HTML `<tr>` containing order metadata. The stream closes when sync is complete or an error occurs.

**Typical flow:** `GET /api/orders/sync-stream/coles` → `GET /api/orders/sync-stream/woolworths` → predictions and price matching can now run.

---

## Predictions

> Analyze order history to forecast when products will run out and when to reorder.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/predictions/refresh` | Recalculate all `ConsumptionPrediction` rows from order history |
| `DELETE` | `/api/predictions/purge` | Delete all predictions |

**`ConsumptionPrediction` fields:** `avg_purchase_interval_days`, `avg_quantity_per_purchase`, `estimated_daily_consumption`, `confidence_score`, `last_purchased_date`, `predicted_runout_date`, `next_purchase_date`.

**Typical flow:** After syncing orders → `POST /api/predictions/refresh` → predictions power shopping list generation.

---

## Prices

> Match equivalent products across stores, track price history, and search/look up products.

### Product Matching

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `POST` | `/api/prices/match-products` | — | Auto-match all unmatched products using fuzzy name matching |
| `POST` | `/api/prices/confirm-match/{match_id}` | `match_id` (path) | Mark a `ProductMatch` as confirmed |
| `POST` | `/api/prices/manual-match` | `coles_id`, `woolworths_id` (form) | Create a manual match between two specific products |
| `POST` | `/api/prices/match/{match_id}/undo` | `match_id` (path) | Restore a previously rejected match |
| `DELETE` | `/api/prices/match/{match_id}` | `match_id` (path) | Reject a match so it's never auto-matched again |
| `DELETE` | `/api/prices/matches/purge` | — | Delete all `ProductMatch` rows |

**`ProductMatch` fields:** `product_a_id`, `product_b_id`, `confidence` (0–1 float), `match_method`, `is_confirmed`, `is_rejected`.

### Price History

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `POST` | `/api/prices/refresh/{store}` | `store` (path) | Kick off background price refresh for a store |
| `GET` | `/api/prices/refresh-progress/{store}` | `store` (path) | Poll for refresh progress (HTMX polling target) |
| `DELETE` | `/api/prices/history/purge` | — | Delete all `PriceHistory` rows |

### Price Charts

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `GET` | `/api/prices/product-history/{product_id}` | `product_id` (path) | HTML chart for a single product's price over time |
| `GET` | `/api/prices/product-history/batch` | `ids` (query, comma-separated) | JSON: multiple product price charts in one request |
| `GET` | `/api/prices/history/{match_id}` | `match_id` (path) | HTML chart comparing Coles vs Woolworths prices for a matched pair |
| `GET` | `/api/prices/history/batch` | `ids` (query, comma-separated) | JSON: multiple match-pair charts in one request |

### Search-Based Matching

Used to manually find a cross-store equivalent for a product that couldn't be auto-matched.

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `GET` | `/api/prices/search-match/{product_id}` | `product_id` (path); `q` (query); `return_to` (query, optional) | Search the opposite store for a matching product |
| `POST` | `/api/prices/search-match/confirm` | `source_product_id`, `store_product_id`, `store`, `name`, `brand`, `unit_size`, `current_price`, `unit_price`, `unit_price_measure`, `image_url`, `product_url`, `return_to` (form) | Upsert the searched product and create a manual match |

### Product Lookup

Search both stores simultaneously without needing to own the product.

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `GET` | `/api/prices/product-lookup/search` | `q` (query) | Search Coles and Woolworths in parallel; returns combined HTML results |
| `POST` | `/api/prices/product-lookup/select` | `store`, `store_product_id`, `name`, `brand`, `unit_size`, `current_price`, `unit_price`, `unit_price_measure`, `image_url`, `product_url` (form) | Upsert a selected product and return the match-search panel |

### Product Visibility

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `POST` | `/api/prices/product/{product_id}/hide` | `product_id` (path) | Mark a product as hidden (no longer buying) |
| `POST` | `/api/prices/product/{product_id}/restore` | `product_id` (path) | Restore a hidden product |
| `DELETE` | `/api/prices/products/purge/{store}` | `store` (path) | Delete all products for a store (cascades to matches, history, predictions) |

### Image Proxy

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `GET` | `/api/prices/image-proxy` | `url` (query) | Proxy product images through the server to bypass CDN hotlink protection |

**Typical matching flow:**
1. `POST /api/prices/match-products` — auto-match as many as possible
2. For unmatched products, `GET /api/prices/search-match/{product_id}?q=<term>` → `POST /api/prices/search-match/confirm`
3. `POST /api/prices/confirm-match/{match_id}` on any pending auto-matches you want to approve
4. `POST /api/prices/refresh/coles` + `POST /api/prices/refresh/woolworths` to populate `PriceHistory`

---

## Shopping List

> Build, manage, and submit shopping lists. Lists transition through states: `DRAFT` → `CONFIRMED` → `ORDERED`.

### List Lifecycle

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `POST` | `/api/shopping-list/new` | — | Create a new empty `DRAFT` shopping list |
| `POST` | `/api/shopping-list/generate` | — | Create a new list pre-populated from predictions |
| `POST` | `/api/shopping-list/add-predictions` | — | Add predicted items to the existing active list (non-destructive) |
| `POST` | `/api/shopping-list/confirm/{list_id}` | `list_id` (path) | Move list to `CONFIRMED`; redirects to `/confirm` |
| `POST` | `/api/shopping-list/close/{list_id}` | `list_id` (path) | Mark list as `ORDERED` |
| `DELETE` | `/api/shopping-list/current` | — | Delete the current `DRAFT` list |
| `DELETE` | `/api/shopping-list/purge` | — | Delete all shopping lists and items |

### List Items

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `GET` | `/api/shopping-list/product-search` | `q` (query) | HTML dropdown of products matching the search term |
| `POST` | `/api/shopping-list/items/add-product` | `product_id` (form) | Add a product to the active list |
| `POST` | `/api/shopping-list/items/{item_id}/quantity` | `item_id` (path); `quantity` (form) | Update item quantity |
| `POST` | `/api/shopping-list/items/{item_id}/store` | `item_id` (path); `store` (form) | Change which store this item is assigned to |
| `DELETE` | `/api/shopping-list/items/{item_id}` | `item_id` (path) | Remove an item from the list |
| `POST` | `/api/shopping-list/copy/{source_list_id}` | `source_list_id` (path) | Copy all items from a past list into the current active list |
| `GET` | `/api/shopping-list/details/{list_id}` | `list_id` (path) | HTML fragment listing all items in a past list |

### Store Assignment

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `POST` | `/api/shopping-list/set-store/{store}` | `store` (path) | Assign all items to a single store |
| `POST` | `/api/shopping-list/submit-store/{store}` | `store` (path) | Assign all to store + confirm; redirects to `/confirm` |
| `POST` | `/api/shopping-list/submit-split` | — | Assign each item to its cheapest store + confirm; redirects to `/confirm` |

**`ShoppingListItem` fields:** `product_id`, `quantity`, `reason`, `coles_price`, `woolworths_price`, `chosen_store`, `is_user_added`, `is_removed`, `is_ordered`.

**Typical flow:**
1. `POST /api/shopping-list/generate` — create list from predictions
2. `GET /api/shopping-list/product-search?q=...` + `POST /api/shopping-list/items/add-product` — add extras
3. `POST /api/shopping-list/items/{id}/quantity` — adjust quantities
4. `POST /api/shopping-list/submit-split` — cheapest-store split + confirm → `/confirm`
5. `GET /api/cart/stream/{store}` — add items to cart

---

## Cart

> Add confirmed shopping list items to a store's cart via the scraper.

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `GET` | `/api/cart/stream/{store}` | `store` (path) | SSE stream; adds items one-by-one and emits status per item |
| `POST` | `/api/cart/add/{store}` | `store` (path) | Non-streaming: add all confirmed items to cart |

**SSE events** from `/api/cart/stream/{store}`: one event per item, containing success/failure status and product name.

**Typical flow:** After confirming a shopping list at `/confirm` → `GET /api/cart/stream/coles` → `GET /api/cart/stream/woolworths`.

---

## Settings

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/settings` | Settings page with connection status per store and data management controls |
| `GET` | `/api/settings/counts` | HTML fragment with row counts for all tables (HTMX polling target) |

---

## Common Workflows

### First-time setup

```
POST /api/auth/import-cookies/coles       # paste cookies from browser
POST /api/auth/import-cookies/woolworths
GET  /api/auth/validate/coles             # confirm they work
GET  /api/auth/validate/woolworths

GET  /api/orders/sync-stream/coles        # sync order history (SSE)
GET  /api/orders/sync-stream/woolworths

POST /api/predictions/refresh             # generate runout forecasts
POST /api/prices/match-products           # auto-match products across stores
POST /api/prices/refresh/coles            # fetch current prices
POST /api/prices/refresh/woolworths
```

### Weekly shop

```
POST /api/shopping-list/generate          # build list from predictions
# review and edit via shopping list UI
POST /api/shopping-list/submit-split      # assign cheapest store per item, confirm → /confirm
GET  /api/cart/stream/coles               # add Coles items to cart (SSE)
GET  /api/cart/stream/woolworths          # add Woolworths items to cart (SSE)
POST /api/shopping-list/close/{list_id}   # mark as ordered
```

### Manually adding a product

```
GET  /api/prices/product-lookup/search?q=oat milk   # search both stores
POST /api/prices/product-lookup/select               # upsert chosen product
# now visible in shopping list product search
GET  /api/shopping-list/product-search?q=oat milk
POST /api/shopping-list/items/add-product            # add to active list
```

### Fixing an unmatched product

```
GET  /api/prices/search-match/{product_id}?q=<search term>   # find equivalent on other store
POST /api/prices/search-match/confirm                         # create the match
POST /api/prices/refresh/coles                                # refresh prices so history records
```

### Refreshing data after a shop

```
GET  /api/orders/sync-stream/coles
GET  /api/orders/sync-stream/woolworths
POST /api/predictions/refresh
POST /api/prices/refresh/coles
POST /api/prices/refresh/woolworths
```
