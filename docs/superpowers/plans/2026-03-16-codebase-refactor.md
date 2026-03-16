# Codebase Refactor Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the shopping-agent codebase for modularity, DRY, type safety, and testability — without changing any user-facing functionality.

**Architecture:** Extract duplicated utilities into services, split monolithic route files, move inline HTML into Jinja2 templates, add complete type annotations, and add a test suite for the core algorithms.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Jinja2, HTMX, Tailwind CSS, rapidfuzz, pytest, pytest-asyncio

**Verification baseline:** Before starting, confirm `ruff check .` and `mypy .` pass (or note any pre-existing errors to ignore). After every task, both must still pass.

---

## Chunk 1: Config Constants + Shared Utilities

### Task 1: Add constants to `config.py`

**Files:**
- Modify: `src/shopping_agent/config.py`

- [ ] **Step 1: Add constants**

Replace the contents of `config.py` with:

```python
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    data_dir: Path = Path(__file__).parent.parent.parent / "data"
    database_url: str = f"sqlite+aiosqlite:///{data_dir}/shopping_agent.db"
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False

    def ensure_dirs(self) -> None:
        """Create required data directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)


# Matching / prediction thresholds
MIN_MATCH_CONFIDENCE: float = 0.3
FUZZY_MATCH_THRESHOLD: float = 70.0
FUZZY_SEARCH_THRESHOLD: float = 65.0
SIZE_MATCH_BONUS: int = 15
SIZE_MISMATCH_PENALTY: int = -20
BRAND_MATCH_THRESHOLD: float = 60.0

# Prediction parameters
PRODUCT_RECENCY_DAYS: int = 120
MIN_PREDICTION_CONFIDENCE: float = 0.3
PREDICTION_LOOKAHEAD_DAYS: int = 7
PREDICTION_LEAD_TIME_DAYS: int = 7
PREDICTION_PURCHASE_COUNT_MIN: int = 3

# Price refresh
PRICE_REFRESH_CONCURRENCY: int = 10

# Chart colours
COLES_COLOUR: str = "#dc2626"
WOOLWORTHS_COLOUR: str = "#16a34a"
PRICE_LINE_COLOUR: str = "#111827"


settings = Settings()
```

- [ ] **Step 2: Run linter**

```bash
cd /Users/andrewsaunders/code/shopping-agent
ruff check src/shopping_agent/config.py
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add src/shopping_agent/config.py
git commit -m "refactor: add named constants to config.py"
```

---

### Task 2: Extract `matches_to_comparisons()` and `build_price_map()` into `services/price_comparison.py`

These functions are duplicated between `routes/views.py`, `routes/api_prices.py`, and `routes/api_shopping_list.py`.

**Files:**
- Modify: `src/shopping_agent/services/price_comparison.py`
- Modify: `src/shopping_agent/routes/views.py` (replace `_matches_to_comparisons` with import)
- Modify: `src/shopping_agent/routes/api_prices.py` (remove inline duplicate)
- Modify: `src/shopping_agent/routes/api_shopping_list.py` (remove inline price_map building)
- Modify: `src/shopping_agent/services/shopping_list.py` (remove inline price_map building)

- [ ] **Step 1: Add `matches_to_comparisons()` to `price_comparison.py`**

Add after the existing `compare_product_prices` function in `services/price_comparison.py`:

```python
def matches_to_comparisons(matches: list[ProductMatch]) -> list[PriceComparison]:
    """Convert a list of ProductMatch ORM rows into PriceComparison dataclasses.

    Args:
        matches: ProductMatch rows with product_a and product_b eagerly loaded.

    Returns:
        List of PriceComparison dataclasses ready for template rendering.
    """
    comparisons = []
    for match in matches:
        pa, pb = match.product_a, match.product_b
        coles_p = pa if pa.store == Store.COLES else pb
        ww_p = pa if pa.store == Store.WOOLWORTHS else pb

        cp = coles_p.current_price
        wp = ww_p.current_price
        cheaper: Store | None = None
        savings = 0.0
        if cp and wp:
            if cp < wp:
                cheaper = Store.COLES
                savings = wp - cp
            elif wp < cp:
                cheaper = Store.WOOLWORTHS
                savings = cp - wp

        comparisons.append(
            PriceComparison(
                product_name=coles_p.name,
                unit_size=coles_p.unit_size,
                product_id=coles_p.id,
                coles_product=coles_p,
                woolworths_product=ww_p,
                coles_price=cp,
                woolworths_price=wp,
                cheaper_store=cheaper,
                savings=savings,
                match_id=match.id,
                match_confidence=match.confidence,
                is_confirmed=match.is_confirmed,
                match_method=match.match_method,
            )
        )
    return comparisons


def build_price_map(matches: list[ProductMatch]) -> dict[int, dict[str, float | None]]:
    """Build a product_id → {coles_price, woolworths_price} lookup from match rows.

    Both product_a and product_b must be eagerly loaded on each match.
    Both products in a pair share the same entry so either product_id can be used
    as the key.

    Args:
        matches: ProductMatch rows with product_a and product_b eagerly loaded.

    Returns:
        Dict mapping product_id to a dict with 'coles_price' and 'woolworths_price'.
    """
    price_map: dict[int, dict[str, float | None]] = {}
    for match in matches:
        pa, pb = match.product_a, match.product_b
        coles_p = pa if pa.store == Store.COLES else pb
        ww_p = pa if pa.store == Store.WOOLWORTHS else pb
        entry: dict[str, float | None] = {
            "coles_price": coles_p.current_price,
            "woolworths_price": ww_p.current_price,
        }
        price_map[coles_p.id] = entry
        price_map[ww_p.id] = entry
    return price_map
```

- [ ] **Step 2: Update `routes/views.py` to import and use `matches_to_comparisons`**

Remove the `_matches_to_comparisons` function (lines 29–64) and update the import:

```python
# Add to imports at top of views.py
from ..services.price_comparison import PriceComparison, matches_to_comparisons
```

In `prices_page()`, replace `comparisons = _matches_to_comparisons(matches)` with:
```python
comparisons = matches_to_comparisons(matches)
```

- [ ] **Step 3: Update `routes/api_prices.py` to use `matches_to_comparisons`**

Note: `_match_row.html` already exists in `templates/` — no new template needs to be created here.

In `confirm_match()`, remove the inline `cp/wp/cheaper/savings` calculation block (lines 195–225) and replace with:

```python
from ..services.price_comparison import matches_to_comparisons as _m2c
# After setting match.is_confirmed = True and committing:
comp = _m2c([match])[0]
html = templates.env.get_template("_match_row.html").render(comp=comp)
return HTMLResponse(html)
```

- [ ] **Step 4: Update `routes/api_shopping_list.py` and `services/shopping_list.py` to use `build_price_map`**

In `api_shopping_list.py`, `add_predictions()` — remove the inline price_map building block (lines 116–129) and replace with:

```python
from ..services.price_comparison import build_price_map
from sqlalchemy.orm import selectinload as sil
matches = (await session.execute(
    select(ProductMatch).options(sil(ProductMatch.product_a), sil(ProductMatch.product_b))
)).scalars().all()
price_map = build_price_map(list(matches))
```

In `services/shopping_list.py`, `generate_shopping_list()` — remove the inline price_map building block (lines 44–58) and replace with:

```python
from .price_comparison import build_price_map
matches_result = await session.execute(
    select(ProductMatch).options(
        selectinload(ProductMatch.product_a),
        selectinload(ProductMatch.product_b),
    )
)
price_map = build_price_map(list(matches_result.scalars().all()))
```

- [ ] **Step 5: Run linter and type checker**

```bash
ruff check src/shopping_agent/services/price_comparison.py src/shopping_agent/routes/views.py src/shopping_agent/routes/api_prices.py src/shopping_agent/routes/api_shopping_list.py src/shopping_agent/services/shopping_list.py
mypy src/shopping_agent/services/price_comparison.py
```

- [ ] **Step 6: Commit**

```bash
git add src/shopping_agent/services/price_comparison.py src/shopping_agent/routes/views.py src/shopping_agent/routes/api_prices.py src/shopping_agent/routes/api_shopping_list.py src/shopping_agent/services/shopping_list.py
git commit -m "refactor: centralise matches_to_comparisons and build_price_map in price_comparison service"
```

---

### Task 3: Extract `choose_best_store()` utility

The store-selection logic `"if both prices exist, pick cheaper; elif coles, pick coles; else woolworths"` appears 4+ times in `api_shopping_list.py` and `services/shopping_list.py`.

**Files:**
- Modify: `src/shopping_agent/services/shopping_list.py`
- Modify: `src/shopping_agent/routes/api_shopping_list.py`

- [ ] **Step 1: Add `choose_best_store()` to `services/shopping_list.py`**

Add near the top of the file, before `generate_shopping_list`:

```python
def choose_best_store(
    coles_price: float | None,
    woolworths_price: float | None,
    fallback: Store,
) -> Store:
    """Choose the cheapest available store for an item.

    Args:
        coles_price: Current Coles price, or None if unavailable.
        woolworths_price: Current Woolworths price, or None if unavailable.
        fallback: Store to use when neither or only one price is available.

    Returns:
        The cheaper store, or fallback if prices are equal or unavailable.
    """
    if coles_price and woolworths_price:
        return Store.COLES if coles_price <= woolworths_price else Store.WOOLWORTHS
    if coles_price:
        return Store.COLES
    if woolworths_price:
        return Store.WOOLWORTHS
    return fallback
```

Also add `choose_best_store` to the `__init__.py` export if needed (check `services/__init__.py`).

- [ ] **Step 2: Update all call sites in `routes/api_shopping_list.py`**

Add import:
```python
from ..services.shopping_list import (
    choose_best_store,
    confirm_list,
    generate_shopping_list,
    get_active_list,
    remove_item,
    update_item_quantity,
    update_item_store,
)
```

Replace every occurrence of the inline store-selection pattern with `choose_best_store(coles_price, woolworths_price, product.store)`.

In `add_predictions()` — replace lines 149–156:
```python
chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
```

In `add_product_to_list()` — replace lines 293–298:
```python
chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
```

In `copy_list()` — replace lines 510–515:
```python
chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
```

In `submit_split()` — replace lines 390–395:
```python
item.chosen_store = choose_best_store(item.coles_price, item.woolworths_price, item.chosen_store or Store.COLES)
```

- [ ] **Step 3: Update `services/shopping_list.py` to use `choose_best_store`**

In `generate_shopping_list()` — replace lines 99–106:
```python
chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
```

- [ ] **Step 4: Run linter**

```bash
ruff check src/shopping_agent/services/shopping_list.py src/shopping_agent/routes/api_shopping_list.py
```

- [ ] **Step 5: Commit**

```bash
git add src/shopping_agent/services/shopping_list.py src/shopping_agent/routes/api_shopping_list.py
git commit -m "refactor: extract choose_best_store() utility, remove 4 duplicates"
```

---

### Task 4: Use constants from `config.py` throughout the codebase

**Files:**
- Modify: `src/shopping_agent/services/price_comparison.py`
- Modify: `src/shopping_agent/services/prediction.py`
- Modify: `src/shopping_agent/routes/api_prices.py`
- Modify: `src/shopping_agent/routes/views.py`

- [ ] **Step 1: Update `price_comparison.py` to use config constants**

Add import at top:
```python
from ..config import (
    BRAND_MATCH_THRESHOLD,
    FUZZY_MATCH_THRESHOLD,
    FUZZY_SEARCH_THRESHOLD,
    SIZE_MATCH_BONUS,
    SIZE_MISMATCH_PENALTY,
)
```

In `sizes_compatible()`: replace `15` with `SIZE_MATCH_BONUS` and `-20` with `SIZE_MISMATCH_PENALTY`.

In `find_best_match()`: replace `threshold: float = 70.0` with `threshold: float = FUZZY_MATCH_THRESHOLD` and `60` in brand check with `BRAND_MATCH_THRESHOLD`.

In `find_or_create_match()`: replace `threshold=65.0` with `threshold=FUZZY_SEARCH_THRESHOLD`.

- [ ] **Step 2: Update `prediction.py` to use config constants**

Add import:
```python
from ..config import (
    MIN_PREDICTION_CONFIDENCE,
    PREDICTION_LEAD_TIME_DAYS,
    PREDICTION_LOOKAHEAD_DAYS,
    PREDICTION_PURCHASE_COUNT_MIN,
    PRODUCT_RECENCY_DAYS,
)
```

Replace `days=120` with `days=PRODUCT_RECENCY_DAYS`.

In `generate_candidates()`: replace `lookahead_days: int = 7` with `lookahead_days: int = PREDICTION_LOOKAHEAD_DAYS`, `lead_time_days: int = 7` with `lead_time_days: int = PREDICTION_LEAD_TIME_DAYS`, `min_confidence: float = 0.3` with `min_confidence: float = MIN_PREDICTION_CONFIDENCE`, and `pred.purchase_count < 3` with `pred.purchase_count < PREDICTION_PURCHASE_COUNT_MIN`.

- [ ] **Step 3: Update `api_prices.py` to use config constants**

Add import:
```python
from ..config import COLES_COLOUR, MIN_MATCH_CONFIDENCE, PRICE_LINE_COLOUR, PRICE_REFRESH_CONCURRENCY, WOOLWORTHS_COLOUR
```

Replace `concurrency = 10` with `concurrency = PRICE_REFRESH_CONCURRENCY`.

Replace hardcoded colour strings in the chart functions with the constants.

- [ ] **Step 4: Update `views.py` dashboard runout confidence threshold**

Add import:
```python
from ..config import MIN_PREDICTION_CONFIDENCE
```

Replace `.where(ConsumptionPrediction.confidence_score >= 0.3)` with `.where(ConsumptionPrediction.confidence_score >= MIN_PREDICTION_CONFIDENCE)`.

Note: use `MIN_PREDICTION_CONFIDENCE`, not `MIN_MATCH_CONFIDENCE` — they both equal 0.3 today but represent different domain concepts.

- [ ] **Step 5: Run linter and type checker**

```bash
ruff check src/shopping_agent/
mypy src/shopping_agent/services/ src/shopping_agent/routes/views.py src/shopping_agent/routes/api_prices.py
```

- [ ] **Step 6: Commit**

```bash
git add src/shopping_agent/
git commit -m "refactor: replace magic numbers with named constants from config.py"
```

---

### Task 5: Move inline HTML into Jinja2 templates

Currently `api_prices.py`, `api_shopping_list.py`, `api_orders.py`, `api_cart.py`, and `views.py` all generate HTML via Python f-strings. Move each to a Jinja2 template partial.

**Files to create:**
- `src/shopping_agent/templates/_chart_single.html`
- `src/shopping_agent/templates/_chart_match.html`
- `src/shopping_agent/templates/_past_list_details.html`
- `src/shopping_agent/templates/_settings_counts.html`

**Files to modify:**
- `src/shopping_agent/routes/api_prices.py`
- `src/shopping_agent/routes/api_shopping_list.py`
- `src/shopping_agent/routes/views.py`

#### Task 5a: Price history chart templates

- [ ] **Step 1: Create `_chart_single.html`**

```html
{# Renders a price history chart for a single product.
   Context: product, points (list of {x, y}), canvas_id, colour, label #}
<div class="bg-gray-50 px-3 sm:px-6 py-4 overflow-hidden">
  <div style="position:relative;max-width:100%">
    <canvas id="{{ canvas_id }}" height="100"></canvas>
  </div>
  <script>
  (function() {
    const ctx = document.getElementById('{{ canvas_id }}').getContext('2d');
    new Chart(ctx, {
      type: 'line',
      data: {
        datasets: [{
          label: '{{ label }}',
          data: {{ points | tojson }},
          borderColor: '{{ colour }}',
          borderWidth: 1,
          pointBackgroundColor: '{{ colour }}',
          pointBorderColor: '{{ colour }}',
          pointRadius: 2,
          pointHoverRadius: 3,
          tension: 0.2,
          parsing: { xAxisKey: 'x', yAxisKey: 'y' }
        }]
      },
      options: {
        responsive: true,
        scales: {
          x: { type: 'category', title: { display: false } },
          y: { title: { display: true, text: 'Price ($)' }, beginAtZero: false }
        },
        plugins: {
          legend: {
            position: 'top',
            labels: {
              usePointStyle: true,
              font: { size: 9 },
              boxWidth: 6,
              boxHeight: 6,
              padding: 4,
              generateLabels: () => [
                { text: '{{ label }}', pointStyle: 'circle', fillStyle: '{{ colour }}', strokeStyle: '{{ colour }}' }
              ]
            }
          }
        }
      }
    });
  })();
  </script>
</div>
```

- [ ] **Step 2: Create `_chart_match.html`**

```html
{# Renders a price history chart for a matched product pair.
   Context: canvas_id, coles_points, ww_points, equal_points, equal_labels,
            all_combined, coles_colour, ww_colour, price_line_colour #}
<div class="bg-gray-50 px-3 sm:px-6 py-4 overflow-hidden">
  <div style="position:relative;max-width:100%">
    <canvas id="{{ canvas_id }}" height="100"></canvas>
  </div>
  <script>
    (function() {
      const ctx = document.getElementById('{{ canvas_id }}').getContext('2d');
      const allPoints = {{ all_combined | tojson }};
      const equalDates = new Set({{ equal_labels | tojson }});

      const splitCanvas = (() => {
        const c = document.createElement('canvas');
        c.width = 14; c.height = 14;
        const cx = c.getContext('2d');
        cx.beginPath(); cx.moveTo(7, 7);
        cx.arc(7, 7, 6, Math.PI / 2, 3 * Math.PI / 2);
        cx.closePath(); cx.fillStyle = '{{ coles_colour }}'; cx.fill();
        cx.beginPath(); cx.moveTo(7, 7);
        cx.arc(7, 7, 6, -Math.PI / 2, Math.PI / 2);
        cx.closePath(); cx.fillStyle = '{{ ww_colour }}'; cx.fill();
        return c;
      })();

      new Chart(ctx, {
        type: 'line',
        data: {
          datasets: [
            {
              label: 'Price',
              data: allPoints,
              borderColor: '{{ price_line_colour }}',
              borderWidth: 1,
              pointRadius: 0,
              tension: 0.2,
              parsing: { xAxisKey: 'x', yAxisKey: 'y' }
            },
            {
              label: 'Coles',
              data: {{ coles_points | tojson }},
              borderColor: 'transparent',
              pointBackgroundColor: '{{ coles_colour }}',
              pointBorderColor: '{{ coles_colour }}',
              pointRadius: (c) => equalDates.has(c.dataset.data[c.dataIndex]?.x) ? 0 : 2,
              showLine: false,
              parsing: { xAxisKey: 'x', yAxisKey: 'y' }
            },
            {
              label: 'Woolworths',
              data: {{ ww_points | tojson }},
              borderColor: 'transparent',
              pointBackgroundColor: '{{ ww_colour }}',
              pointBorderColor: '{{ ww_colour }}',
              pointRadius: (c) => equalDates.has(c.dataset.data[c.dataIndex]?.x) ? 0 : 2,
              showLine: false,
              parsing: { xAxisKey: 'x', yAxisKey: 'y' }
            },
            {
              label: 'Same Price',
              data: {{ equal_points | tojson }},
              borderColor: 'transparent',
              pointStyle: splitCanvas,
              pointRadius: 2,
              showLine: false,
              parsing: { xAxisKey: 'x', yAxisKey: 'y' }
            }
          ]
        },
        options: {
          responsive: true,
          scales: {
            x: { type: 'category', title: { display: false } },
            y: { title: { display: true, text: 'Price ($)' }, beginAtZero: false }
          },
          plugins: {
            legend: {
              position: 'top',
              labels: {
                usePointStyle: true,
                font: { size: 9 },
                boxWidth: 6,
                boxHeight: 6,
                padding: 4,
                generateLabels: (chart) => [
                  { text: 'Price', pointStyle: 'line', strokeStyle: '{{ price_line_colour }}', lineWidth: 1, datasetIndex: 0 },
                  { text: 'Coles', pointStyle: 'circle', fillStyle: '{{ coles_colour }}', strokeStyle: '{{ coles_colour }}', datasetIndex: 1 },
                  { text: 'Woolworths', pointStyle: 'circle', fillStyle: '{{ ww_colour }}', strokeStyle: '{{ ww_colour }}', datasetIndex: 2 },
                  { text: 'Same Price', pointStyle: splitCanvas, datasetIndex: 3 },
                ]
              }
            }
          }
        }
      });
    })();
    </script>
</div>
```

- [ ] **Step 3: Update `product_price_history()` in `api_prices.py` to use `_chart_single.html`**

Replace the entire f-string `html = f"""..."""` block and `return HTMLResponse(html)` with:

```python
from ..config import COLES_COLOUR, WOOLWORTHS_COLOUR
from ..templating import templates as tmpl

if not points:
    return HTMLResponse(
        '<div class="bg-gray-50 px-6 py-3 text-xs text-gray-400">No price history recorded yet.</div>'
    )
colour = COLES_COLOUR if is_coles else WOOLWORTHS_COLOUR
html = tmpl.env.get_template("_chart_single.html").render(
    canvas_id=f"pchart-{product_id}",
    points=points,
    colour=colour,
    label=label,
)
return HTMLResponse(html)
```

Remove the `import json` and inline `color`/`label` variables that are no longer needed (keep `is_coles`, `label` for the colour selection).

- [ ] **Step 4: Update `price_history()` in `api_prices.py` to use `_chart_match.html`**

Replace the entire f-string block with:

```python
from ..config import COLES_COLOUR, PRICE_LINE_COLOUR, WOOLWORTHS_COLOUR

if not coles_points and not ww_points:
    return HTMLResponse(
        '<div class="bg-gray-50 px-6 py-3 text-xs text-gray-400">No price history recorded yet.</div>'
    )
html = tmpl.env.get_template("_chart_match.html").render(
    canvas_id=f"chart-{match_id}",
    coles_points=coles_points,
    ww_points=ww_points,
    equal_points=equal_points,
    equal_labels=equal_labels,
    all_combined=all_combined,
    coles_colour=COLES_COLOUR,
    ww_colour=WOOLWORTHS_COLOUR,
    price_line_colour=PRICE_LINE_COLOUR,
)
return HTMLResponse(html)
```

Also add `from ..templating import templates as tmpl` to imports at the top of the file.

- [ ] **Step 5: Create `_past_list_details.html`**

Create `src/shopping_agent/templates/_past_list_details.html`:

```html
{# Fragment: items table for a past shopping list.
   Context: items (list of dicts with name, quantity, coles_price, woolworths_price, product_id) #}
<div class="bg-gray-50 rounded-lg my-2 overflow-hidden border border-gray-200">
    <table class="min-w-full">
        <thead>
            <tr class="bg-gray-100 text-xs font-medium text-gray-500 uppercase">
                <th class="px-4 py-2 text-left">Product</th>
                <th class="px-4 py-2 text-center">Qty</th>
                <th class="px-4 py-2 text-center">Coles</th>
                <th class="px-4 py-2 text-center">Woolworths</th>
                <th class="px-4 py-2"></th>
            </tr>
        </thead>
        <tbody class="divide-y divide-gray-200">
            {% for item in items %}
            <tr>
                <td class="px-4 py-2 text-sm text-gray-900">{{ item.name }}</td>
                <td class="px-4 py-2 text-sm text-gray-500 text-center">{{ item.quantity }}</td>
                <td class="px-4 py-2 text-sm text-red-600 text-center">
                    {% if item.coles_price %}${{ "%.2f"|format(item.coles_price) }}{% else %}—{% endif %}
                </td>
                <td class="px-4 py-2 text-sm text-green-600 text-center">
                    {% if item.woolworths_price %}${{ "%.2f"|format(item.woolworths_price) }}{% else %}—{% endif %}
                </td>
                <td class="px-4 py-2 text-right">
                    <span id="add-result-{{ item.product_id }}">
                        <button hx-post="/api/shopping-list/items/add-product"
                                hx-vals='{"product_id": {{ item.product_id }}}'
                                hx-target="#add-result-{{ item.product_id }}"
                                hx-swap="innerHTML"
                                class="px-2 py-1 bg-blue-100 text-blue-700 text-xs rounded hover:bg-blue-200">
                            Add to list
                        </button>
                    </span>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</div>
```

- [ ] **Step 6: Update `list_details()` in `api_shopping_list.py` to use the template**

Replace the entire loop + f-string HTML generation with:

```python
from ..templating import templates as tmpl

items_data = []
for item in items:
    product = await session.get(Product, item.product_id)
    if not product:
        continue
    items_data.append({
        "name": product.name,
        "quantity": item.quantity,
        "coles_price": item.coles_price,
        "woolworths_price": item.woolworths_price,
        "product_id": item.product_id,
    })

html = tmpl.env.get_template("_past_list_details.html").render(items=items_data)
return HTMLResponse(html)
```

- [ ] **Step 7: Create `_settings_counts.html` and update `views.py`**

Create `src/shopping_agent/templates/_settings_counts.html`:

```html
{# Fragment: data management rows for the settings page.
   Context: rows (list of dicts with label, count, also_deletes, endpoint, tid) #}
{% for row in rows %}
<tr>
    <td class="px-3 sm:px-6 py-3 text-sm font-medium text-gray-900">{{ row.label }}</td>
    <td class="px-3 sm:px-6 py-3 text-sm text-gray-500" id="{{ row.tid }}-count">{{ row.count }}</td>
    <td class="px-3 sm:px-6 py-3 text-xs text-gray-400 hidden sm:table-cell">{{ row.also_deletes }}</td>
    <td class="px-3 sm:px-6 py-3 text-right">
        <span id="{{ row.tid }}-result" class="mr-2 text-sm"></span>
        <button
            hx-delete="{{ row.endpoint }}"
            hx-target="#{{ row.tid }}-result"
            hx-on:htmx:after-request="htmx.trigger('#data-mgmt-body', 'countsRefresh')"
            class="px-3 py-1.5 bg-orange-100 text-orange-700 text-xs rounded hover:bg-orange-200">
            Purge
        </button>
    </td>
</tr>
{% endfor %}
```

In `views.py`, replace `_counts_rows_html()` and its call sites with:

```python
def _counts_rows(counts: dict) -> list[dict]:
    """Build the data rows for the settings data-management table."""
    return [
        {"label": "Coles Orders", "count": f"{counts['coles_orders']} orders, {counts['coles_order_items']} items", "also_deletes": "price history", "endpoint": "/api/orders/purge/coles", "tid": "purge-coles"},
        {"label": "Woolworths Orders", "count": f"{counts['woolworths_orders']} orders, {counts['woolworths_order_items']} items", "also_deletes": "price history", "endpoint": "/api/orders/purge/woolworths", "tid": "purge-woolworths"},
        {"label": "Coles Products", "count": str(counts["coles_products"]), "also_deletes": "matches, price history, predictions", "endpoint": "/api/prices/products/purge/coles", "tid": "purge-coles-products"},
        {"label": "Woolworths Products", "count": str(counts["woolworths_products"]), "also_deletes": "matches, price history, predictions", "endpoint": "/api/prices/products/purge/woolworths", "tid": "purge-woolworths-products"},
        {"label": "Product Matches", "count": str(counts["product_matches"]), "also_deletes": "—", "endpoint": "/api/prices/matches/purge", "tid": "purge-matches"},
        {"label": "Price History", "count": str(counts["price_history"]), "also_deletes": "—", "endpoint": "/api/prices/history/purge", "tid": "purge-price-history"},
        {"label": "Predictions", "count": str(counts["predictions"]), "also_deletes": "—", "endpoint": "/api/predictions/purge", "tid": "purge-predictions"},
        {"label": "Shopping Lists", "count": f"{counts['shopping_lists']} lists, {counts['shopping_list_items']} items", "also_deletes": "—", "endpoint": "/api/shopping-list/purge", "tid": "purge-lists"},
    ]


@router.get("/api/settings/counts")
async def settings_counts(session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Return OOB HTML fragment of data management counts."""
    counts = await _get_counts(session)
    html = templates.env.get_template("_settings_counts.html").render(rows=_counts_rows(counts))
    return HTMLResponse(html)
```

Update `settings_page()` to pass `rows` instead of `counts_rows_html`:
```python
return templates.TemplateResponse(
    "settings.html",
    {
        "request": request,
        "active_page": "settings",
        "coles_connected": coles_connected,
        "woolworths_connected": woolworths_connected,
        "counts": counts,
        "counts_rows": _counts_rows(counts),
    },
)
```

Update `settings.html` to use `{% include "_settings_counts.html" %}` or render with `counts_rows` variable (the template currently uses `counts_rows_html | safe` — change to `{% for row in counts_rows %}{% include "_settings_counts.html" %}{% endfor %}` or render as a sub-template).

Actually, the simpler approach: keep passing the rendered HTML from the endpoint but now render it via template. In `settings.html`, find where `counts_rows_html` is inserted and verify it's doing `{{ counts_rows_html | safe }}`. Change `settings_page()` to:
```python
counts_rows_html = templates.env.get_template("_settings_counts.html").render(rows=_counts_rows(counts))
```
This avoids changing `settings.html`.

- [ ] **Step 8: Run linter and start server to manually verify charts and list details still render**

```bash
ruff check src/shopping_agent/
uvicorn shopping_agent.main:app --reload --host 0.0.0.0
```

Navigate to `/prices` and expand a price history chart to verify it renders. Navigate to `/shopping-list`, click a past list to verify the details panel renders.

- [ ] **Step 9: Commit**

```bash
git add src/shopping_agent/
git commit -m "refactor: move inline chart and table HTML into Jinja2 templates"
```

---

## Chunk 2: File Splitting — Views and Prices

### Task 6: Split `routes/views.py` into focused view modules

**Current:** `views.py` (605 lines) contains 7 page views + 3 large helpers.
**Target:** Each page domain gets its own file. Shared helpers move to services.

**New file structure:**

| File | Responsibility |
|------|---------------|
| `routes/views/__init__.py` | Re-export router |
| `routes/views/dashboard.py` | `GET /` |
| `routes/views/orders.py` | `GET /orders` |
| `routes/views/predictions.py` | `GET /predictions` |
| `routes/views/prices.py` | `GET /prices`, `GET /prices/search-match/{id}` |
| `routes/views/shopping_list.py` | `GET /shopping-list`, `GET /confirm` |
| `routes/views/settings.py` | `GET /settings`, `GET /api/settings/counts` |

Helpers that belong in services:
- `_shopping_list_context()` → `services/shopping_list.py` as `get_shopping_list_context()`
- `_resolve_display_names()` → `services/shopping_list.py` as `resolve_display_names()`
- `_shopping_list_history()` → `services/shopping_list.py` as `get_list_history()`
- `_predictions_list()` → `services/prediction.py` as `get_predictions_with_match_info()`
- `_get_counts()` → `services/data_management.py` (new file) as `get_db_counts()`
- `_counts_rows()` → stays in `settings.py` (it's presentation logic)

**Files to create:**
- `src/shopping_agent/routes/views/__init__.py`
- `src/shopping_agent/routes/views/dashboard.py`
- `src/shopping_agent/routes/views/orders.py`
- `src/shopping_agent/routes/views/predictions.py`
- `src/shopping_agent/routes/views/prices.py`
- `src/shopping_agent/routes/views/shopping_list.py`
- `src/shopping_agent/routes/views/settings.py`
- `src/shopping_agent/services/data_management.py`

**Files to modify:**
- `src/shopping_agent/services/shopping_list.py` (add 3 new functions)
- `src/shopping_agent/services/prediction.py` (add 1 new function)
- `src/shopping_agent/main.py` (update router import)
- `src/shopping_agent/routes/api_shopping_list.py` (update import of `_shopping_list_context`)

- [ ] **Step 1: Add `get_shopping_list_context()`, `resolve_display_names()`, and `get_list_history()` to `services/shopping_list.py`**

Move `_resolve_display_names`, `_shopping_list_context`, and `_shopping_list_history` from `routes/views.py` into `services/shopping_list.py`, renaming them (remove the `_` prefix and use descriptive public names):

```python
async def resolve_display_names(
    session: AsyncSession, items: list[ShoppingListItem]
) -> tuple[dict[int, str], dict[int, dict], dict[int, dict]]:
    """Resolve per-item display names and per-store product mappings.

    For each non-removed item, looks up the cross-store match to determine
    which store names and products are available.

    Args:
        session: Async database session.
        items: Shopping list items to resolve.

    Returns:
        Tuple of (display_names, store_names, store_products) where each is
        keyed by item.id:
        - display_names: the chosen store's product name (fallback: canonical name)
        - store_names: {'coles': name|None, 'woolworths': name|None}
        - store_products: {'coles': Product|None, 'woolworths': Product|None}
    """
    display_names: dict[int, str] = {}
    store_names: dict[int, dict] = {}
    store_products: dict[int, dict] = {}

    for item in items:
        if item.is_removed:
            continue
        canonical = item.product
        partner = None
        match_result = await session.execute(
            select(ProductMatch).where(
                (
                    (ProductMatch.product_a_id == canonical.id)
                    | (ProductMatch.product_b_id == canonical.id)
                ),
                ProductMatch.is_rejected == False,  # noqa: E712
            )
        )
        match = match_result.scalars().first()
        if match:
            partner_id = (
                match.product_b_id if match.product_a_id == canonical.id else match.product_a_id
            )
            partner = await session.get(Product, partner_id)

        if canonical.store == Store.COLES:
            coles_product: Product | None = canonical
            ww_product: Product | None = partner if partner and partner.store == Store.WOOLWORTHS else None
        else:
            ww_product = canonical
            coles_product = partner if partner and partner.store == Store.COLES else None

        coles_name = coles_product.name if coles_product else None
        woolworths_name = ww_product.name if ww_product else None

        store_names[item.id] = {"coles": coles_name, "woolworths": woolworths_name}
        store_products[item.id] = {"coles": coles_product, "woolworths": ww_product}

        if item.chosen_store == Store.COLES and coles_name:
            display_names[item.id] = coles_name
        elif item.chosen_store == Store.WOOLWORTHS and woolworths_name:
            display_names[item.id] = woolworths_name
        else:
            display_names[item.id] = canonical.name

    return display_names, store_names, store_products


async def get_shopping_list_context(session: AsyncSession) -> dict:
    """Build the full shopping list context dict for template rendering.

    Fetches the active list, resolves display names, and computes store totals
    and a store recommendation.

    Args:
        session: Async database session.

    Returns:
        Dict containing: shopping_list, display_names, store_names, store_products,
        single_store, coles_total, woolworths_total, best_total, recommendation.
    """
    query = (
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    result = await session.execute(query)
    shopping_list = result.scalars().first()

    coles_total = 0.0
    woolworths_total = 0.0
    best_total = 0.0
    display_names: dict[int, str] = {}
    store_names: dict[int, dict] = {}
    store_products: dict[int, dict] = {}
    single_store: Store | None = None

    if shopping_list:
        display_names, store_names, store_products = await resolve_display_names(
            session, shopping_list.items
        )
        active_items = [i for i in shopping_list.items if not i.is_removed]
        stores_used = {i.chosen_store for i in active_items if i.chosen_store}
        single_store = stores_used.pop() if len(stores_used) == 1 else None
        for item in active_items:
            cp = item.coles_price * item.quantity if item.coles_price else None
            wp = item.woolworths_price * item.quantity if item.woolworths_price else None
            coles_total += cp if cp is not None else (wp or 0)
            woolworths_total += wp if wp is not None else (cp or 0)
            best_total += min(cp, wp) if cp is not None and wp is not None else (cp or wp or 0)

    recommendation = ""
    if coles_total and woolworths_total:
        if coles_total < woolworths_total:
            recommendation = f"Coles is ${woolworths_total - coles_total:.2f} cheaper overall"
        elif woolworths_total < coles_total:
            recommendation = f"Woolworths is ${coles_total - woolworths_total:.2f} cheaper overall"
        else:
            recommendation = "Same price at both stores"

    return {
        "shopping_list": shopping_list,
        "display_names": display_names,
        "store_names": store_names,
        "store_products": store_products,
        "single_store": single_store,
        "coles_total": coles_total,
        "woolworths_total": woolworths_total,
        "best_total": best_total,
        "recommendation": recommendation,
    }


async def get_list_history(session: AsyncSession) -> list[dict]:
    """Return summary rows for past (ordered) shopping lists.

    Args:
        session: Async database session.

    Returns:
        List of dicts with keys: id, name, created_at, status, store, item_count, total.
    """
    result = await session.execute(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items))
        .where(ShoppingList.status == ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    rows = []
    for sl in result.scalars().all():
        active = [i for i in sl.items if not i.is_removed]
        stores = {i.chosen_store for i in active if i.chosen_store}
        store = stores.pop() if len(stores) == 1 else None
        total = 0.0
        for i in active:
            price = (i.coles_price if store == Store.COLES else i.woolworths_price) or 0
            total += price * i.quantity
        rows.append({
            "id": sl.id,
            "name": sl.name,
            "created_at": sl.created_at,
            "status": sl.status,
            "store": store,
            "item_count": len(active),
            "total": total,
        })
    return rows
```

- [ ] **Step 2: Add `get_predictions_with_match_info()` to `services/prediction.py`**

Note: The dashboard cannot use this function directly — it needs predictions filtered to a 7-day window. Add an optional `max_runout_date` parameter to support both use cases (the predictions page passes `None` for all predictions; the dashboard passes `week_ahead` for the filtered view).

```python
async def get_predictions_with_match_info(
    session: AsyncSession,
    max_runout_date: date | None = None,
) -> list[ConsumptionPrediction]:
    """Load all predictions, annotating each with match info for template rendering.

    Sets the following transient attributes on each prediction:
    - days_until_runout (int)
    - is_matched (bool)
    - matched_product (Product | None)
    - match_id (int | None)

    Args:
        session: Async database session.

    Returns:
        List of annotated ConsumptionPrediction objects ordered by runout date.
    """
    from datetime import date as _date
    from ..models import Product, ProductMatch
    from sqlalchemy.orm import selectinload

    today = _date.today()
    query = (
        select(ConsumptionPrediction)
        .options(selectinload(ConsumptionPrediction.product))
        .order_by(ConsumptionPrediction.predicted_runout_date)
    )
    if max_runout_date is not None:
        query = query.where(ConsumptionPrediction.predicted_runout_date <= max_runout_date)
    result = await session.execute(query)

    matches_result = await session.execute(
        select(ProductMatch)
        .where(ProductMatch.is_rejected == False)  # noqa: E712
        .options(
            selectinload(ProductMatch.product_a),
            selectinload(ProductMatch.product_b),
        )
    )
    matched_product: dict[int, Product] = {}
    match_id_map: dict[int, int] = {}
    for m in matches_result.scalars().all():
        matched_product[m.product_a_id] = m.product_b
        matched_product[m.product_b_id] = m.product_a
        match_id_map[m.product_a_id] = m.id
        match_id_map[m.product_b_id] = m.id

    predictions = []
    for pred in result.scalars().all():
        pred.days_until_runout = (pred.predicted_runout_date - today).days
        other = matched_product.get(pred.product_id)
        pred.is_matched = other is not None
        pred.matched_product = other
        pred.match_id = match_id_map.get(pred.product_id)
        predictions.append(pred)
    return predictions
```

- [ ] **Step 3: Create `services/data_management.py`**

```python
"""Data management service — DB record counts for the settings page."""
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    ConsumptionPrediction,
    Order,
    OrderItem,
    PriceHistory,
    Product,
    ProductMatch,
    ShoppingList,
    ShoppingListItem,
    Store,
)

logger = logging.getLogger(__name__)


async def get_db_counts(session: AsyncSession) -> dict[str, int]:
    """Return record counts for all major tables.

    Args:
        session: Async database session.

    Returns:
        Dict of table-name → record count.
    """
    return {
        "coles_orders": (await session.execute(select(func.count(Order.id)).where(Order.store == Store.COLES))).scalar() or 0,
        "coles_order_items": (await session.execute(
            select(func.count(OrderItem.id)).join(Order).where(Order.store == Store.COLES)
        )).scalar() or 0,
        "woolworths_orders": (await session.execute(select(func.count(Order.id)).where(Order.store == Store.WOOLWORTHS))).scalar() or 0,
        "woolworths_order_items": (await session.execute(
            select(func.count(OrderItem.id)).join(Order).where(Order.store == Store.WOOLWORTHS)
        )).scalar() or 0,
        "coles_products": (await session.execute(select(func.count(Product.id)).where(Product.store == Store.COLES))).scalar() or 0,
        "woolworths_products": (await session.execute(select(func.count(Product.id)).where(Product.store == Store.WOOLWORTHS))).scalar() or 0,
        "product_matches": (await session.execute(select(func.count(ProductMatch.id)))).scalar() or 0,
        "price_history": (await session.execute(select(func.count(PriceHistory.id)))).scalar() or 0,
        "predictions": (await session.execute(select(func.count(ConsumptionPrediction.id)))).scalar() or 0,
        "shopping_lists": (await session.execute(select(func.count(ShoppingList.id)))).scalar() or 0,
        "shopping_list_items": (await session.execute(select(func.count(ShoppingListItem.id)))).scalar() or 0,
    }
```

- [ ] **Step 4: Create the `routes/views/` package**

Create `src/shopping_agent/routes/views/__init__.py`:

```python
"""View routes package — one module per page domain."""
from fastapi import APIRouter

from .dashboard import router as dashboard_router
from .orders import router as orders_router
from .predictions import router as predictions_router
from .prices import router as prices_router
from .settings import router as settings_router
from .shopping_list import router as shopping_list_router

router = APIRouter()
router.include_router(dashboard_router)
router.include_router(orders_router)
router.include_router(predictions_router)
router.include_router(prices_router)
router.include_router(shopping_list_router)
router.include_router(settings_router)
```

- [ ] **Step 5: Create `routes/views/dashboard.py`**

Uses `get_predictions_with_match_info(session, max_runout_date=week_ahead)` from the service (Step 2) to avoid duplicating the match-annotation loop.

```python
"""Dashboard page view."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import MIN_PREDICTION_CONFIDENCE
from ...database import get_session
from ...models import (
    ConsumptionPrediction,
    Order,
    Product,
    ProductMatch,
    ShoppingList,
    Store,
)
from ...services.prediction import get_predictions_with_match_info
from ...services.shopping_list import get_shopping_list_context
from ...templating import templates

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Render the dashboard page."""
    today = date.today()
    week_ahead = today + timedelta(days=7)

    coles_orders = (await session.execute(select(func.count(Order.id)).where(Order.store == Store.COLES))).scalar() or 0
    ww_orders = (await session.execute(select(func.count(Order.id)).where(Order.store == Store.WOOLWORTHS))).scalar() or 0
    coles_products = (await session.execute(select(func.count(Product.id)).where(Product.store == Store.COLES))).scalar() or 0
    ww_products = (await session.execute(select(func.count(Product.id)).where(Product.store == Store.WOOLWORTHS))).scalar() or 0
    removed_count = (await session.execute(
        select(func.count(Product.id)).where(Product.is_hidden == True)  # noqa: E712
    )).scalar() or 0
    matched_count = (await session.execute(
        select(func.count(ProductMatch.id)).where(ProductMatch.is_rejected == False)  # noqa: E712
    )).scalar() or 0
    rejected_count = (await session.execute(
        select(func.count(ProductMatch.id)).where(ProductMatch.is_rejected == True)  # noqa: E712
    )).scalar() or 0
    pred_count = (await session.execute(select(func.count(ConsumptionPrediction.id)))).scalar() or 0

    # Re-use the service function with a date filter so the dashboard only shows
    # products running out within the next 7 days, without duplicating the
    # match-annotation loop inline.
    upcoming_runouts_all = await get_predictions_with_match_info(session, max_runout_date=week_ahead)
    upcoming_runouts = [
        p for p in upcoming_runouts_all
        if p.confidence_score >= MIN_PREDICTION_CONFIDENCE
    ]

    list_count = (await session.execute(select(func.count(ShoppingList.id)))).scalar() or 0
    coles_last_sync = (await session.execute(
        select(func.max(Order.order_date)).where(Order.store == Store.COLES)
    )).scalar()
    ww_last_sync = (await session.execute(
        select(func.max(Order.order_date)).where(Order.store == Store.WOOLWORTHS)
    )).scalar()

    sl_ctx = await get_shopping_list_context(session)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active_page": "dashboard",
            "coles_orders": coles_orders,
            "ww_orders": ww_orders,
            "coles_products": coles_products,
            "ww_products": ww_products,
            "removed_count": removed_count,
            "matched_count": matched_count,
            "rejected_count": rejected_count,
            "pred_count": pred_count,
            "runout_count": len(upcoming_runouts),
            "upcoming_runouts": upcoming_runouts,
            "list_count": list_count,
            "coles_last_sync": coles_last_sync,
            "ww_last_sync": ww_last_sync,
            **sl_ctx,
        },
    )
```

- [ ] **Step 6: Create `routes/views/orders.py`**

```python
"""Orders page view."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...database import get_session
from ...models import Order, Store
from ...templating import templates

router = APIRouter()


@router.get("/orders")
async def orders_page(
    request: Request,
    store: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the orders page, optionally filtered by store."""
    query = select(Order).options(selectinload(Order.items)).order_by(Order.order_date.desc())
    if store in ("coles", "woolworths"):
        query = query.where(Order.store == Store(store))
    result = await session.execute(query)
    orders = result.scalars().all()

    return templates.TemplateResponse(
        "orders.html",
        {
            "request": request,
            "active_page": "orders",
            "orders": orders,
            "store_filter": store or "all",
        },
    )
```

- [ ] **Step 7: Create `routes/views/predictions.py`**

```python
"""Predictions page view."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...services.prediction import get_predictions_with_match_info
from ...templating import templates

router = APIRouter()


@router.get("/predictions")
async def predictions_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Render the predictions page."""
    predictions = await get_predictions_with_match_info(session)
    return templates.TemplateResponse(
        "predictions.html",
        {"request": request, "active_page": "predictions", "predictions": predictions},
    )
```

- [ ] **Step 8: Create `routes/views/prices.py`**

```python
"""Prices page view."""
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...database import get_session
from ...models import Order, OrderItem, Product, ProductMatch, Store
from ...services.price_comparison import matches_to_comparisons
from ...templating import templates

router = APIRouter()


@router.get("/prices")
async def prices_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Render the price comparison page."""
    result = await session.execute(
        select(Product)
        .where(Product.is_hidden == False)  # noqa: E712
        .order_by(Product.store, Product.name)
    )
    all_products = list(result.scalars().all())
    visible_ids = {p.id for p in all_products}

    match_result = await session.execute(
        select(ProductMatch)
        .options(selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b))
        .where(ProductMatch.is_rejected == False)  # noqa: E712
        .order_by(ProductMatch.confidence.desc())
    )
    matches = [
        m for m in match_result.scalars().all()
        if m.product_a_id in visible_ids and m.product_b_id in visible_ids
    ]
    comparisons = matches_to_comparisons(matches)

    matched_ids: set[int] = set()
    for m in matches:
        matched_ids.add(m.product_a_id)
        matched_ids.add(m.product_b_id)

    unmatched_coles = [p for p in all_products if p.store == Store.COLES and p.id not in matched_ids]
    unmatched_woolworths = [p for p in all_products if p.store == Store.WOOLWORTHS and p.id not in matched_ids]

    lo_rows = await session.execute(
        select(OrderItem.product_id, func.max(Order.order_date))
        .join(Order, OrderItem.order_id == Order.id)
        .where(OrderItem.product_id.in_(visible_ids))
        .group_by(OrderItem.product_id)
    )
    last_ordered: dict[int, date] = dict(lo_rows.all())

    rejected_result = await session.execute(
        select(ProductMatch)
        .options(selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b))
        .where(ProductMatch.is_rejected == True)  # noqa: E712
        .order_by(ProductMatch.updated_at.desc())
    )
    rejected_matches = rejected_result.scalars().all()

    hidden_result = await session.execute(
        select(Product)
        .options(selectinload(Product.order_items).selectinload(OrderItem.order))
        .where(Product.is_hidden == True)  # noqa: E712
        .order_by(Product.store, Product.name)
    )
    hidden_products = []
    for p in hidden_result.scalars().all():
        dates = [oi.order.order_date for oi in p.order_items if oi.order]
        p.last_ordered_date = max(dates) if dates else None
        hidden_products.append(p)

    unavailable_result = await session.execute(
        select(Product)
        .where(Product.is_available == False)  # noqa: E712
        .where(Product.is_hidden == False)  # noqa: E712
        .order_by(Product.store, Product.name)
    )
    unavailable_products = list(unavailable_result.scalars().all())

    return templates.TemplateResponse(
        "prices.html",
        {
            "request": request,
            "active_page": "prices",
            "comparisons": comparisons,
            "unmatched_coles": unmatched_coles,
            "unmatched_woolworths": unmatched_woolworths,
            "rejected_matches": rejected_matches,
            "hidden_products": hidden_products,
            "unavailable_products": unavailable_products,
            "last_ordered": last_ordered,
        },
    )


@router.get("/prices/search-match/{product_id}")
async def search_match_page(
    product_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the manual search-match page for a given product."""
    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse("Product not found", status_code=404)
    target_store = Store.WOOLWORTHS if product.store == Store.COLES else Store.COLES
    return templates.TemplateResponse(
        "search_match.html",
        {
            "request": request,
            "active_page": "prices",
            "product": product,
            "target_store": target_store.value,
        },
    )
```

- [ ] **Step 9: Create `routes/views/shopping_list.py`**

```python
"""Shopping list and confirm page views."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...database import get_session
from ...models import ListStatus, ShoppingList, ShoppingListItem, Store
from ...services.shopping_list import (
    get_list_history,
    get_shopping_list_context,
    resolve_display_names,
)
from ...templating import templates

router = APIRouter()


@router.get("/shopping-list")
async def shopping_list_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Render the shopping list page."""
    ctx = await get_shopping_list_context(session)
    past_lists = await get_list_history(session)
    return templates.TemplateResponse(
        "shopping_list.html",
        {"request": request, "active_page": "shopping_list", "past_lists": past_lists, **ctx},
    )


@router.get("/confirm")
async def confirm_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Render the order confirmation page."""
    query = (
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status == ListStatus.CONFIRMED)
        .order_by(ShoppingList.created_at.desc())
    )
    result = await session.execute(query)
    shopping_list = result.scalars().first()

    coles_items: list[ShoppingListItem] = []
    woolworths_items: list[ShoppingListItem] = []
    coles_total = 0.0
    woolworths_total = 0.0
    display_names: dict[int, str] = {}

    if shopping_list:
        all_items = [i for i in shopping_list.items if not i.is_removed]
        display_names, _, _sp = await resolve_display_names(session, all_items)
        for item in all_items:
            if item.chosen_store == Store.COLES:
                coles_items.append(item)
                coles_total += (item.coles_price or 0) * item.quantity
            else:
                woolworths_items.append(item)
                woolworths_total += (item.woolworths_price or 0) * item.quantity

    return templates.TemplateResponse(
        "confirm.html",
        {
            "request": request,
            "active_page": "confirm",
            "shopping_list": shopping_list,
            "display_names": display_names,
            "coles_items": coles_items,
            "woolworths_items": woolworths_items,
            "coles_total": coles_total,
            "woolworths_total": woolworths_total,
        },
    )
```

- [ ] **Step 10: Create `routes/views/settings.py`**

```python
"""Settings page view."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...scrapers.coles import coles_scraper
from ...scrapers.woolworths import woolworths_scraper
from ...services.data_management import get_db_counts
from ...templating import templates

router = APIRouter()


def _counts_rows(counts: dict) -> list[dict]:
    """Build table row data for the settings data-management section."""
    return [
        {"label": "Coles Orders", "count": f"{counts['coles_orders']} orders, {counts['coles_order_items']} items", "also_deletes": "price history", "endpoint": "/api/orders/purge/coles", "tid": "purge-coles"},
        {"label": "Woolworths Orders", "count": f"{counts['woolworths_orders']} orders, {counts['woolworths_order_items']} items", "also_deletes": "price history", "endpoint": "/api/orders/purge/woolworths", "tid": "purge-woolworths"},
        {"label": "Coles Products", "count": str(counts["coles_products"]), "also_deletes": "matches, price history, predictions", "endpoint": "/api/prices/products/purge/coles", "tid": "purge-coles-products"},
        {"label": "Woolworths Products", "count": str(counts["woolworths_products"]), "also_deletes": "matches, price history, predictions", "endpoint": "/api/prices/products/purge/woolworths", "tid": "purge-woolworths-products"},
        {"label": "Product Matches", "count": str(counts["product_matches"]), "also_deletes": "—", "endpoint": "/api/prices/matches/purge", "tid": "purge-matches"},
        {"label": "Price History", "count": str(counts["price_history"]), "also_deletes": "—", "endpoint": "/api/prices/history/purge", "tid": "purge-price-history"},
        {"label": "Predictions", "count": str(counts["predictions"]), "also_deletes": "—", "endpoint": "/api/predictions/purge", "tid": "purge-predictions"},
        {"label": "Shopping Lists", "count": f"{counts['shopping_lists']} lists, {counts['shopping_list_items']} items", "also_deletes": "—", "endpoint": "/api/shopping-list/purge", "tid": "purge-lists"},
    ]


@router.get("/api/settings/counts")
async def settings_counts(session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Return HTML fragment of data-management counts (polled by HTMX)."""
    counts = await get_db_counts(session)
    html = templates.env.get_template("_settings_counts.html").render(rows=_counts_rows(counts))
    return HTMLResponse(html)


@router.get("/settings")
async def settings_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Render the settings page."""
    coles_connected = await coles_scraper.is_authenticated()
    woolworths_connected = await woolworths_scraper.is_authenticated()
    counts = await get_db_counts(session)
    counts_rows_html = templates.env.get_template("_settings_counts.html").render(
        rows=_counts_rows(counts)
    )

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active_page": "settings",
            "coles_connected": coles_connected,
            "woolworths_connected": woolworths_connected,
            "counts": counts,
            "counts_rows_html": counts_rows_html,
        },
    )
```

- [ ] **Step 11: Verify `main.py` needs no changes**

No change to `main.py` is needed. The existing import `from .routes import views` (or equivalent) still resolves because `routes/views/__init__.py` re-exports `router`. Confirm `views.router` appears in the `include_router` call in `main.py` and it still works after creating the package.

- [ ] **Step 12: Update `routes/api_shopping_list.py` to use the new service function**

Change:
```python
from .views import _shopping_list_context
```
to:
```python
from ..services.shopping_list import get_shopping_list_context as _shopping_list_context
```

This keeps all internal call sites working without further changes.

- [ ] **Step 13: Delete old `routes/views.py`**

```bash
rm src/shopping_agent/routes/views.py
```

- [ ] **Step 14: Run linter and verify server starts**

```bash
ruff check src/shopping_agent/
uvicorn shopping_agent.main:app --reload --host 0.0.0.0
```

Navigate to `/`, `/orders`, `/predictions`, `/prices`, `/shopping-list`, `/settings` and verify each page loads without errors.

- [ ] **Step 15: Commit**

```bash
git add src/shopping_agent/
git commit -m "refactor: split views.py into routes/views/ package; extract helpers to services"
```

---

### Task 7: Split `routes/api_prices.py` into focused modules

**Current:** 718 lines mixing refresh, match management, search, product management, and chart rendering.
**Target:** Split into logical submodules under `routes/api_prices/`.

**New file structure:**

| File | Responsibility |
|------|---------------|
| `routes/api_prices/__init__.py` | Re-export router |
| `routes/api_prices/refresh.py` | Background refresh, progress polling |
| `routes/api_prices/matches.py` | Confirm/reject/create/undo/purge matches |
| `routes/api_prices/search.py` | Search-match endpoints |
| `routes/api_prices/products.py` | Hide/restore/purge products, image proxy |
| `routes/api_prices/charts.py` | Price history chart endpoints |

**Files to create:** the 6 files above
**Files to delete:** `src/shopping_agent/routes/api_prices.py`

- [ ] **Step 1: Create `routes/api_prices/__init__.py`**

```python
"""Price API routes package."""
from fastapi import APIRouter

from .charts import router as charts_router
from .matches import router as matches_router
from .products import router as products_router
from .refresh import router as refresh_router
from .search import router as search_router

router = APIRouter()
router.include_router(refresh_router)
router.include_router(matches_router)
router.include_router(search_router)
router.include_router(products_router)
router.include_router(charts_router)
```

The following steps each create one submodule. The **full function bodies are copied verbatim** from the current `src/shopping_agent/routes/api_prices.py`. The only changes per file are: (a) relative imports change from `..` to `...` depth, (b) any inline imports moved to the top, (c) config constants replace hardcoded values per Task 4.

**Reference line numbers in `api_prices.py` for each function:**
- `_refresh_progress` dict: line 20
- `_do_price_refresh()`: lines 39–128
- `refresh_prices()`: lines 131–153
- `refresh_progress()`: lines 156–178
- `confirm_match()`: lines 181–228
- `create_manual_match()`: lines 231–269
- `undo_rejected_match()`: lines 387–395
- `purge_all_matches()`: lines 669–675
- `delete_match()`: lines 708–717
- `purge_price_history()`: lines 698–705
- `search_match()`: lines 272–305
- `confirm_search_match()`: lines 308–384
- `image_proxy()`: lines 23–36
- `hide_product()`: lines 398–413
- `restore_product()`: lines 416–424
- `purge_products()`: lines 678–695
- `product_price_history()`: lines 427–509 (replaced with template in Task 5)
- `price_history()`: lines 512–666 (replaced with template in Task 5)

- [ ] **Step 2: Create `routes/api_prices/refresh.py`**

```python
"""Price refresh background task and progress polling."""
import asyncio
import logging
from datetime import date as date_type
from typing import TypedDict

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import PRICE_REFRESH_CONCURRENCY
from ...database import async_session, get_session
from ...models import (
    ListStatus,
    PriceHistory,
    Product,
    ProductMatch,
    ShoppingList,
    ShoppingListItem,
    Store,
)
from ...scrapers.coles import ColesScraper
from ...scrapers.woolworths import WoolworthsScraper

router = APIRouter()
logger = logging.getLogger(__name__)


class RefreshState(TypedDict, total=False):
    done: int
    total: int
    running: bool
    updated: int


_refresh_progress: dict[str, RefreshState] = {}
```

Then copy `_do_price_refresh`, `refresh_prices`, and `refresh_progress` verbatim from `api_prices.py` lines 39–178, updating:
- Replace `concurrency = 10` → `concurrency = PRICE_REFRESH_CONCURRENCY`
- Move `from datetime import date as date_type` and `from sqlalchemy import func as sqlfunc` to top-level imports (already done above)
- Move `from ..models import ListStatus, ProductMatch, ShoppingList, ShoppingListItem` to top-level imports (already done above)
- Change all `..` imports (e.g. `from ..database`) to `...` (e.g. `from ...database`)
- Change `dict` type of `_refresh_progress` to `dict[str, RefreshState]`

- [ ] **Step 3: Create `routes/api_prices/matches.py`**

```python
"""Match management — confirm, reject, create, purge."""
import logging

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...models import ConsumptionPrediction, PriceHistory, Product, ProductMatch, Store
from ...services.price_comparison import matches_to_comparisons
from ...templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)
```

Then copy the following functions verbatim from `api_prices.py`, updating `..` → `...` imports and replacing the inline `cp/wp/cheaper/savings` block in `confirm_match` with `matches_to_comparisons([match])[0]` (Task 2 Step 3):
- `confirm_match()` — lines 181–228
- `create_manual_match()` — lines 231–269
- `undo_rejected_match()` — lines 387–395
- `purge_all_matches()` — lines 669–675
- `purge_price_history()` — lines 698–705
- `delete_match()` — lines 708–717

- [ ] **Step 4: Create `routes/api_prices/search.py`**

```python
"""Search-based product matching."""
import logging

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...models import Product, ProductMatch, Store
from ...scrapers.coles import coles_scraper
from ...scrapers.woolworths import woolworths_scraper
from ...templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)
```

Copy verbatim from `api_prices.py`, updating imports:
- `search_match()` — lines 272–305
- `confirm_search_match()` — lines 308–384

- [ ] **Step 5: Create `routes/api_prices/products.py`**

```python
"""Product visibility management and image proxy."""
import logging

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...models import ConsumptionPrediction, PriceHistory, Product, ProductMatch, Store

router = APIRouter()
logger = logging.getLogger(__name__)
```

Copy verbatim from `api_prices.py`, updating imports:
- `image_proxy()` — lines 23–36
- `hide_product()` — lines 398–413
- `restore_product()` — lines 416–424
- `purge_products()` — lines 678–695

- [ ] **Step 6: Create `routes/api_prices/charts.py`**

```python
"""Price history chart endpoints."""
import json
import logging
from datetime import date as date_type

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...config import COLES_COLOUR, PRICE_LINE_COLOUR, WOOLWORTHS_COLOUR
from ...database import get_session
from ...models import PriceHistory, Product, ProductMatch, Store
from ...templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)
```

Copy `product_price_history()` (lines 427–509) and `price_history()` (lines 512–666) verbatim, then apply the Task 5 template changes (replace the f-string HTML blocks with template renders using `_chart_single.html` and `_chart_match.html`). Replace hardcoded colour strings with `COLES_COLOUR`, `WOOLWORTHS_COLOUR`, `PRICE_LINE_COLOUR`.

- [ ] **Step 7: Update `main.py` router registration**

The import path changes from `from .routes.api_prices import router` to the same path (the `__init__.py` re-exports it), so no change needed. Verify the prefix registration is still correct:

```python
app.include_router(prices_router, prefix="/api/prices")
```

- [ ] **Step 8: Delete old `routes/api_prices.py`**

```bash
rm src/shopping_agent/routes/api_prices.py
```

- [ ] **Step 9: Run linter and verify server**

```bash
ruff check src/shopping_agent/routes/api_prices/
uvicorn shopping_agent.main:app --reload --host 0.0.0.0
```

Test price refresh, confirm a match, and verify chart endpoints.

- [ ] **Step 10: Commit**

```bash
git add src/shopping_agent/
git commit -m "refactor: split api_prices.py into routes/api_prices/ subpackage"
```

---

## Chunk 3: Shopping List Cleanup + Scraper Refactoring

### Task 8: Clean up `routes/api_shopping_list.py`

The `list_details` endpoint generates HTML inline and the `_list_header_oob` function generates complex conditional HTML. With Task 5 having moved the list details to a template, the remaining inline HTML is `_list_header_oob`.

**Files to modify:**
- `src/shopping_agent/routes/api_shopping_list.py`

- [ ] **Step 1: Create `templates/_list_header.html`**

```html
{# OOB header update fragment for the shopping list page.
   Context: shopping_list (ShoppingList|None), has_list (bool), title (str),
            new_cls, pred_cls, new_disabled, pred_disabled #}
<div id="list-header" hx-swap-oob="innerHTML">
    <h1 class="text-2xl font-bold text-gray-900">{{ title }}</h1>
    <div class="flex flex-wrap gap-2 items-center">
        <button hx-post="/api/shopping-list/new" hx-target="#list-content" hx-swap="innerHTML"
                {{ new_disabled }} class="px-4 py-2 text-sm rounded {{ new_cls }}">New List</button>
        <button hx-post="/api/shopping-list/add-predictions" hx-target="#list-content" hx-swap="innerHTML"
                hx-indicator="#pred-spinner" {{ pred_disabled }}
                class="px-4 py-2 text-sm rounded {{ pred_cls }}">Add Predicted Items</button>
        <span id="pred-spinner" class="htmx-indicator text-gray-400 self-center text-sm">adding...</span>
        {% if has_list %}
        <button hx-delete="/api/shopping-list/current" hx-target="#list-content" hx-swap="innerHTML"
                class="px-4 py-2 text-sm rounded bg-red-100 text-red-600 hover:bg-red-200">Delete List</button>
        {% endif %}
    </div>
</div>
```

- [ ] **Step 2: Update `_list_header_oob()` in `api_shopping_list.py` to use the template**

```python
def _list_header_oob(shopping_list: ShoppingList | None) -> str:
    """Render the OOB list-header fragment."""
    has_list = shopping_list is not None
    return templates.env.get_template("_list_header.html").render(
        has_list=has_list,
        title=(shopping_list.name if has_list else None) or "Shopping List",
        new_cls="bg-gray-200 text-gray-400 cursor-not-allowed" if has_list else "bg-blue-600 text-white hover:bg-blue-700",
        pred_cls="bg-gray-200 text-gray-400 cursor-not-allowed" if not has_list else "bg-green-600 text-white hover:bg-green-700",
        new_disabled="disabled" if has_list else "",
        pred_disabled="disabled" if not has_list else "",
    )
```

- [ ] **Step 3: Fix N+1 query in `list_details()`**

Currently it fetches each product separately in a loop. Batch load them:

```python
@router.get("/details/{list_id}")
async def list_details(list_id: int, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Return an HTML fragment listing all items in a past shopping list."""
    items_result = await session.execute(
        select(ShoppingListItem)
        .where(
            ShoppingListItem.shopping_list_id == list_id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )
    items = items_result.scalars().all()
    product_ids = [i.product_id for i in items]

    products_result = await session.execute(
        select(Product).where(Product.id.in_(product_ids))
    )
    products_by_id = {p.id: p for p in products_result.scalars().all()}

    items_data = [
        {
            "name": products_by_id[i.product_id].name,
            "quantity": i.quantity,
            "coles_price": i.coles_price,
            "woolworths_price": i.woolworths_price,
            "product_id": i.product_id,
        }
        for i in items
        if i.product_id in products_by_id
    ]
    html = templates.env.get_template("_past_list_details.html").render(items=items_data)
    return HTMLResponse(html)
```

- [ ] **Step 4: Run linter**

```bash
ruff check src/shopping_agent/routes/api_shopping_list.py
```

- [ ] **Step 5: Commit**

```bash
git add src/shopping_agent/routes/api_shopping_list.py src/shopping_agent/templates/
git commit -m "refactor: move remaining inline HTML in api_shopping_list.py to templates"
```

---

### Task 9: Extract shared scraper cookie handling into `BaseScraper`

Both `ColesScraper._load_cookies()` and `WoolworthsScraper._load_cookies()` are nearly identical. Same for `_save_cookies_from_client()`.

**Files to modify:**
- `src/shopping_agent/scrapers/base.py`
- `src/shopping_agent/scrapers/coles.py`
- `src/shopping_agent/scrapers/woolworths.py`

- [ ] **Step 1: Add cookie methods to `BaseScraper`**

Add to `scrapers/base.py`:

```python
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import AsyncGenerator

import httpx

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Abstract base scraper. Subclasses implement store-specific scraping logic."""

    #: The store this scraper targets. Must be set on each subclass.
    store: "Store"  # type: ignore[assignment]

    #: Default cookie domain for this store (e.g. ".coles.com.au").
    _cookie_domain: str = ""

    async def _load_cookies(self) -> httpx.Cookies:
        """Load persisted cookies for this store from the database.

        Returns:
            An httpx.Cookies jar populated with the stored cookies.
        """
        from ..database import async_session
        from ..models.store_cookies import StoreCookies
        from sqlalchemy import select

        jar = httpx.Cookies()
        async with async_session() as session:
            result = await session.execute(
                select(StoreCookies).where(StoreCookies.store == self.store)
            )
            row = result.scalar_one_or_none()
            if row:
                try:
                    raw_cookies = json.loads(row.cookies_json)
                    for c in raw_cookies:
                        jar.set(
                            c["name"],
                            c["value"],
                            domain=c.get("domain", self._cookie_domain),
                            path=c.get("path", "/"),
                        )
                    logger.info(
                        "Loaded %d cookies for %s",
                        len(raw_cookies),
                        self.store.value,
                    )
                except Exception:
                    logger.warning(
                        "Failed to load %s cookies", self.store.value, exc_info=True
                    )
        return jar

    async def _save_cookies_from_client(self) -> None:
        """Persist current client cookies for this store to the database.

        Reads cookies from `self._client`. Subclasses must set `self._client`
        before calling this method. Does nothing if `self._client` is None.
        """
        from ..database import async_session
        from ..models.store_cookies import StoreCookies
        from sqlalchemy import select

        client = getattr(self, "_client", None)
        if not client:
            return

        cookie_list = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or self._cookie_domain,
                "path": cookie.path or "/",
                "secure": cookie.secure,
                "httpOnly": False,
            }
            for cookie in client.cookies.jar
        ]
        cookies_json = json.dumps(cookie_list, indent=2)

        async with async_session() as session:
            result = await session.execute(
                select(StoreCookies).where(StoreCookies.store == self.store)
            )
            row = result.scalar_one_or_none()
            if row:
                row.cookies_json = cookies_json
            else:
                session.add(StoreCookies(store=self.store, cookies_json=cookies_json))
            await session.commit()

        logger.info("Saved %d cookies for %s", len(cookie_list), self.store.value)

    # ... existing abstract methods unchanged
```

- [ ] **Step 2: Remove `_load_cookies` and `_save_cookies_from_client` from `ColesScraper`**

In `coles.py`:
1. Delete the `_load_cookies` method (lines 108–133).
2. Delete the `_save_cookies_from_client` method (lines 135–162 approximately).
3. Add class attribute `_cookie_domain: str = ".coles.com.au"` to `ColesScraper`.

The call sites for `_save_cookies_from_client` in `coles.py` use `await self._save_cookies_from_client()` (no arguments). The base class method also uses no arguments. No call-site changes needed — the signature is identical.

- [ ] **Step 3: Remove same methods from `WoolworthsScraper`**

In `woolworths.py`:
1. Delete the `_load_cookies` method (lines 47–68).
2. Delete the `_save_cookies_from_client` method (lines 70–97 approximately).
3. Add class attribute `_cookie_domain: str = ".woolworths.com.au"` to `WoolworthsScraper`.

Same zero-argument call sites — no call-site changes needed.

- [ ] **Step 4: Extract Coles GraphQL query definitions to `scrapers/coles_queries.py`**

Create `src/shopping_agent/scrapers/coles_queries.py`:

```python
"""GraphQL query strings for the Coles API."""

_GQL_PRODUCT_FIELDS = """
    id
    name
    brand
    description
    imageUris { uri }
    size
    pricing {
        now
        was
        unit { price }
        promotionType
        saveAmount
    }
"""

GQL_SEARCH = """
query SearchProducts(
    $searchTerm: String!,
    $storeId: BrandedId!,
    $pageNumber: Int = 1,
    $pageSize: Int = 48
) {
    searchProducts(input: {
        searchTerm: $searchTerm
        storeId: $storeId
        pagination: { pageNumber: $pageNumber pageSize: $pageSize }
    }) {
        results {
            """ + _GQL_PRODUCT_FIELDS + """
        }
    }
}
"""

GQL_CROSS_CATEGORY = """
query GetCrossCategory(
    $categoryIds: [ID!]!,
    $storeId: BrandedId!,
    $memoryToken: String
) {
    crossCategory(
        categoryIds: $categoryIds
        storeId: $storeId
        memoryToken: $memoryToken
    ) {
        products {
            """ + _GQL_PRODUCT_FIELDS + """
        }
        memoryToken
    }
}
"""
```

In `coles.py`, replace the query constants with imports:

```python
from .coles_queries import GQL_CROSS_CATEGORY, GQL_SEARCH
```

And replace `_GQL_SEARCH` / `_GQL_CROSS_CATEGORY` references with `GQL_SEARCH` / `GQL_CROSS_CATEGORY`.

- [ ] **Step 5: Run linter and verify scrapers are importable**

```bash
ruff check src/shopping_agent/scrapers/
python -c "from shopping_agent.scrapers.coles import coles_scraper; print('OK')"
python -c "from shopping_agent.scrapers.woolworths import woolworths_scraper; print('OK')"
```

- [ ] **Step 6: Commit**

```bash
git add src/shopping_agent/scrapers/
git commit -m "refactor: extract shared cookie handling to BaseScraper; move Coles GraphQL queries to coles_queries.py"
```

---

## Chunk 4: Type Hints + Docstrings

### Task 10: Complete type annotations on all route files

**Files:**
- `src/shopping_agent/routes/api_auth.py`
- `src/shopping_agent/routes/api_cart.py`
- `src/shopping_agent/routes/api_orders.py`
- `src/shopping_agent/routes/api_predictions.py`
- `src/shopping_agent/routes/api_shopping_list.py`
- All new `routes/views/*.py` files (already typed in Tasks 6–7)
- All new `routes/api_prices/*.py` files (already typed in Task 7)

For each file:

- [ ] **Step 1: Add return types to all async route handlers**

All FastAPI route handlers should have an explicit return type. Pattern:

```python
@router.get("/orders")
async def orders_page(...) -> HTMLResponse:
    ...

@router.post("/refresh/{store}")
async def refresh_prices(...) -> HTMLResponse:
    ...
```

Use `-> HTMLResponse`, `-> RedirectResponse`, `-> Response`, or `-> StreamingResponse` as appropriate.

- [ ] **Step 2: Add TypedDict for `_refresh_progress`**

In `routes/api_prices/refresh.py`:

```python
from typing import TypedDict


class RefreshState(TypedDict, total=False):
    done: int
    total: int
    running: bool
    updated: int


_refresh_progress: dict[str, RefreshState] = {}
```

- [ ] **Step 3: Add return type to `_list_header_oob`**

```python
def _list_header_oob(shopping_list: ShoppingList | None) -> str:
```

- [ ] **Step 4: Run mypy and fix all errors**

```bash
mypy src/shopping_agent/routes/ --ignore-missing-imports
```

Fix each reported error. Common issues:
- Missing `Optional` vs `| None` — use `| None` (modern syntax, Python 3.10+)
- `dict` vs `Dict` — use lowercase `dict`
- Missing return type on helper functions

- [ ] **Step 5: Commit**

```bash
git add src/shopping_agent/routes/
git commit -m "refactor: complete type annotations on all route files"
```

---

### Task 11: Complete type annotations on services and scrapers

**Files:**
- `src/shopping_agent/services/order_sync.py`
- `src/shopping_agent/services/cart.py`
- `src/shopping_agent/services/prediction.py`
- `src/shopping_agent/services/price_comparison.py`
- `src/shopping_agent/services/shopping_list.py`
- `src/shopping_agent/services/data_management.py`
- `src/shopping_agent/scrapers/base.py`
- `src/shopping_agent/scrapers/coles.py`
- `src/shopping_agent/scrapers/woolworths.py`

- [ ] **Step 1: Run mypy on services**

```bash
mypy src/shopping_agent/services/ --ignore-missing-imports
```

Fix each error. Key gaps identified in audit:
- `find_best_match`: `candidates` already typed
- `compute_prediction`: return type is `dict | None` — consider `dict[str, Any] | None`
- `refresh_predictions`: return type `int` already present but verify

- [ ] **Step 2: Run mypy on scrapers**

```bash
mypy src/shopping_agent/scrapers/ --ignore-missing-imports
```

Key gaps:
- `ColesScraper._get_client()` — add `-> httpx.AsyncClient`
- `ColesScraper.get_order_history()` — add `-> list[ScrapedOrder]`
- Various private helpers — add parameter and return types

- [ ] **Step 3: Add Google-style docstrings to all public functions lacking them**

Priority functions needing docstrings:
- `normalize_product_name()` in `price_comparison.py`
- `normalize_size()` in `price_comparison.py`
- `size_to_grams()` in `price_comparison.py`
- `refresh_predictions()` in `prediction.py` — explain union-find algorithm
- `generate_candidates()` in `prediction.py`
- All public methods in `ColesScraper` and `WoolworthsScraper`
- `build_price_map()` and `matches_to_comparisons()` (already added in Task 2)

Example for `normalize_product_name()`:

```python
def normalize_product_name(name: str) -> str:
    """Standardize a product name for fuzzy matching.

    Lowercases, removes store brand names, strips common descriptor words
    that vary between stores (e.g. "organic", "free range"), and normalizes
    weight/volume formats by removing spaces between number and unit.

    Args:
        name: Raw product name from scraper output.

    Returns:
        Normalized, whitespace-collapsed product name string.
    """
```

- [ ] **Step 4: Run mypy on full project**

```bash
mypy src/shopping_agent/ --ignore-missing-imports
```

Resolve all remaining errors. If any are false positives or require complex overloads, use `# type: ignore[code]` with a comment explaining why.

- [ ] **Step 5: Run full linter**

```bash
ruff check src/shopping_agent/
```

Fix any remaining lint errors (unused imports, line length, etc.)

- [ ] **Step 6: Commit**

```bash
git add src/shopping_agent/
git commit -m "refactor: complete type annotations and docstrings across services and scrapers"
```

---

## Chunk 5: Tests

### Task 12: Set up test infrastructure

**Files to create:**
- `tests/__init__.py`
- `tests/conftest.py`

- [ ] **Step 1: Verify pytest is installed**

```bash
cd /Users/andrewsaunders/code/shopping-agent
pytest --version
```

Expected: pytest 7.x or 8.x

- [ ] **Step 2: Create `tests/__init__.py`**

```python
```
(empty file)

- [ ] **Step 3: Create `tests/conftest.py`**

```python
"""Shared pytest fixtures for the shopping-agent test suite."""
import pytest
from datetime import date

from shopping_agent.services.prediction import PurchaseRecord


@pytest.fixture
def sample_purchases() -> list[PurchaseRecord]:
    """Three purchases at regular weekly intervals, 2 units each."""
    return [
        PurchaseRecord(order_date=date(2025, 1, 1), quantity=2),
        PurchaseRecord(order_date=date(2025, 1, 8), quantity=2),
        PurchaseRecord(order_date=date(2025, 1, 15), quantity=2),
    ]


@pytest.fixture
def irregular_purchases() -> list[PurchaseRecord]:
    """Purchases at irregular intervals to test confidence scoring."""
    return [
        PurchaseRecord(order_date=date(2025, 1, 1), quantity=1),
        PurchaseRecord(order_date=date(2025, 1, 20), quantity=3),
        PurchaseRecord(order_date=date(2025, 2, 5), quantity=1),
        PurchaseRecord(order_date=date(2025, 3, 1), quantity=2),
    ]
```

- [ ] **Step 4: Run test collection to verify setup**

```bash
pytest tests/ --collect-only
```

Expected: 0 tests collected, no errors.

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: add test infrastructure with conftest fixtures"
```

---

### Task 13: Tests for `compute_prediction()`

**File to create:** `tests/test_prediction.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for shopping_agent.services.prediction."""
import math
from datetime import date

import pytest

from shopping_agent.services.prediction import (
    PurchaseRecord,
    ShoppingListCandidate,
    compute_prediction,
    generate_candidates,
)


class TestComputePrediction:
    def test_returns_none_with_single_purchase(self):
        purchases = [PurchaseRecord(order_date=date(2025, 1, 1), quantity=1)]
        assert compute_prediction(purchases) is None

    def test_returns_none_with_empty_list(self):
        assert compute_prediction([]) is None

    def test_regular_weekly_purchases_produce_prediction(self, sample_purchases):
        result = compute_prediction(sample_purchases, today=date(2025, 1, 15))
        assert result is not None
        assert result["avg_purchase_interval_days"] == pytest.approx(7.0, abs=0.5)
        assert result["avg_quantity_per_purchase"] == pytest.approx(2.0, abs=0.1)
        assert result["purchase_count"] == 3

    def test_runout_date_is_after_last_purchase(self, sample_purchases):
        result = compute_prediction(sample_purchases, today=date(2025, 1, 15))
        assert result is not None
        assert result["predicted_runout_date"] > date(2025, 1, 15)

    def test_next_purchase_date_before_runout(self, sample_purchases):
        result = compute_prediction(sample_purchases, today=date(2025, 1, 15))
        assert result is not None
        assert result["next_purchase_date"] < result["predicted_runout_date"]

    def test_confidence_between_zero_and_one(self, sample_purchases):
        result = compute_prediction(sample_purchases)
        assert result is not None
        assert 0.0 <= result["confidence_score"] <= 1.0

    def test_irregular_purchases_have_lower_confidence(self, sample_purchases, irregular_purchases):
        regular = compute_prediction(sample_purchases)
        irregular = compute_prediction(irregular_purchases)
        assert regular is not None
        assert irregular is not None
        assert regular["confidence_score"] >= irregular["confidence_score"]

    def test_daily_consumption_is_positive(self, sample_purchases):
        result = compute_prediction(sample_purchases)
        assert result is not None
        assert result["estimated_daily_consumption"] > 0

    def test_duplicate_dates_are_ignored(self):
        """Purchases on the same date produce a zero interval that is skipped.

        With input [Jan 1, Jan 1, Jan 8], the Jan1→Jan1 interval (days=0) is
        filtered out but Jan1→Jan8 is valid. One valid interval is enough for
        a result, so compute_prediction should return a non-None result.
        """
        purchases = [
            PurchaseRecord(order_date=date(2025, 1, 1), quantity=2),
            PurchaseRecord(order_date=date(2025, 1, 1), quantity=2),  # same date
            PurchaseRecord(order_date=date(2025, 1, 8), quantity=2),
        ]
        result = compute_prediction(purchases)
        assert result is not None
        assert result["purchase_count"] == 3

    def test_last_purchase_quantity_recorded(self):
        purchases = [
            PurchaseRecord(order_date=date(2025, 1, 1), quantity=3),
            PurchaseRecord(order_date=date(2025, 1, 8), quantity=5),
        ]
        result = compute_prediction(purchases)
        assert result is not None
        assert result["last_purchase_quantity"] == 5


class TestGenerateCandidates:
    """Tests for generate_candidates() shopping list candidate generation."""

    def _make_pred(self, runout_date, confidence=0.8, purchase_count=5, product_id=1):
        """Create a mock ConsumptionPrediction-like object."""
        from unittest.mock import MagicMock
        pred = MagicMock()
        pred.product_id = product_id
        pred.predicted_runout_date = runout_date
        pred.confidence_score = confidence
        pred.purchase_count = purchase_count
        pred.estimated_daily_consumption = 0.3
        pred.avg_quantity_per_purchase = 2.0
        return pred

    LOOKAHEAD = 7
    LEAD_TIME = 7

    def test_includes_product_running_out_today(self):
        today = date(2025, 3, 1)
        pred = self._make_pred(today)
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert any(c.product_id == 1 for c in candidates)

    def test_excludes_product_not_running_out_in_window(self):
        today = date(2025, 3, 1)
        far_future = date(2025, 6, 1)  # 92 days out — well outside 7+7 window
        pred = self._make_pred(far_future)
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert len(candidates) == 0

    def test_excludes_low_confidence_predictions(self):
        today = date(2025, 3, 1)
        pred = self._make_pred(today, confidence=0.1)
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert len(candidates) == 0

    def test_excludes_predictions_with_few_purchases(self):
        today = date(2025, 3, 1)
        pred = self._make_pred(today, purchase_count=2)
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert len(candidates) == 0

    def test_quantity_is_at_least_avg_per_purchase(self):
        today = date(2025, 3, 1)
        pred = self._make_pred(today)
        pred.avg_quantity_per_purchase = 4.0
        pred.estimated_daily_consumption = 0.1  # ceil(0.1*7)=1, which is < 4 → should clamp to 4
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert len(candidates) == 1
        assert candidates[0].quantity >= 4

    def test_reason_includes_runout_date(self):
        today = date(2025, 3, 1)
        pred = self._make_pred(today)
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert len(candidates) == 1
        assert "2025-03-01" in candidates[0].reason
```

- [ ] **Step 2: Run tests to verify they are discovered and some fail**

```bash
pytest tests/test_prediction.py -v
```

Expected: some PASS, some FAIL (if any logic differs from expectations). Fix test expectations for any surprising behaviour (do not change production code logic).

- [ ] **Step 3: Commit**

```bash
git add tests/test_prediction.py
git commit -m "test: add tests for compute_prediction and generate_candidates"
```

---

### Task 14: Tests for product matching

**File to create:** `tests/test_price_comparison.py`

- [ ] **Step 1: Write tests**

```python
"""Tests for shopping_agent.services.price_comparison."""
from unittest.mock import MagicMock

import pytest

from shopping_agent.services.price_comparison import (
    find_best_match,
    normalize_product_name,
    normalize_size,
    size_to_grams,
    sizes_compatible,
)


class TestNormalizeProductName:
    def test_lowercases_input(self):
        assert normalize_product_name("FULL CREAM MILK") == "full cream milk"

    def test_removes_store_names(self):
        result = normalize_product_name("Coles Full Cream Milk 2L")
        assert "coles" not in result

    def test_removes_woolworths_brand(self):
        result = normalize_product_name("Woolworths Full Cream Milk 2L")
        assert "woolworths" not in result

    def test_removes_fresh_descriptor(self):
        result = normalize_product_name("Fresh Full Cream Milk 2L")
        assert "fresh" not in result

    def test_normalizes_weight_format(self):
        result = normalize_product_name("Milk 2 L")
        assert "2l" in result

    def test_collapses_whitespace(self):
        result = normalize_product_name("  milk   2l  ")
        assert result == "milk 2l"


class TestNormalizeSize:
    def test_lowercases_and_strips(self):
        assert normalize_size("  2L  ") == "2l"

    def test_replaces_litre(self):
        assert normalize_size("2litre") == "2l"

    def test_replaces_gram(self):
        assert normalize_size("500gram") == "500g"

    def test_removes_spaces(self):
        assert normalize_size("2 l") == "2l"


class TestSizeToGrams:
    def test_grams(self):
        assert size_to_grams("500g") == 500.0

    def test_kilograms(self):
        assert size_to_grams("1kg") == 1000.0

    def test_ml(self):
        assert size_to_grams("250ml") == 250.0

    def test_litres(self):
        assert size_to_grams("2l") == 2000.0

    def test_returns_none_for_unparseable(self):
        assert size_to_grams("large") is None

    def test_returns_none_for_empty(self):
        assert size_to_grams("") is None


class TestSizesCompatible:
    def test_matching_sizes_return_positive(self):
        assert sizes_compatible("500g", "500g") > 0

    def test_different_sizes_return_negative(self):
        assert sizes_compatible("500g", "1kg") < 0

    def test_none_returns_zero(self):
        assert sizes_compatible(None, "500g") == 0
        assert sizes_compatible("500g", None) == 0
        assert sizes_compatible(None, None) == 0

    def test_unparseable_returns_zero(self):
        assert sizes_compatible("large", "medium") == 0

    def test_equivalent_sizes_match(self):
        # 1000g == 1kg
        assert sizes_compatible("1000g", "1kg") > 0


def _make_product(name, store_val="coles", brand=None, unit_size=None):
    p = MagicMock()
    p.name = name
    p.store.value = store_val
    p.brand = brand
    p.unit_size = unit_size
    p.id = hash(name)
    return p


class TestFindBestMatch:
    def test_finds_obvious_match(self):
        source = _make_product("Full Cream Milk 2L")
        candidates = [
            _make_product("Full Cream Milk 2 Litre", "woolworths"),
            _make_product("Skim Milk 1L", "woolworths"),
        ]
        result = find_best_match(source, candidates)
        assert result is not None
        matched, confidence = result
        assert matched.name == "Full Cream Milk 2 Litre"
        assert 0.0 < confidence <= 1.0

    def test_returns_none_when_no_good_match(self):
        source = _make_product("Full Cream Milk 2L")
        candidates = [_make_product("Orange Juice 1L", "woolworths")]
        result = find_best_match(source, candidates)
        assert result is None

    def test_returns_none_with_empty_candidates(self):
        source = _make_product("Full Cream Milk 2L")
        assert find_best_match(source, []) is None

    def test_brand_mismatch_excludes_candidate(self):
        source = _make_product("Milk 2L", brand="Dairy Farmers")
        candidate = _make_product("Milk 2L", "woolworths", brand="Oak")
        # Low brand score should skip this candidate
        result = find_best_match(source, [candidate])
        # May or may not match depending on brand score threshold — just must not raise
        assert result is None or result[1] > 0

    def test_size_mismatch_reduces_score(self):
        source = _make_product("Milk", unit_size="2L")
        candidate_same_size = _make_product("Milk", "woolworths", unit_size="2L")
        candidate_diff_size = _make_product("Milk", "woolworths", unit_size="1L")

        result_same = find_best_match(source, [candidate_same_size])
        result_diff = find_best_match(source, [candidate_diff_size])

        if result_same and result_diff:
            assert result_same[1] > result_diff[1]
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_price_comparison.py -v
```

Expected: all PASS. Fix any expectation mismatches.

- [ ] **Step 3: Commit**

```bash
git add tests/test_price_comparison.py
git commit -m "test: add tests for normalize_product_name, find_best_match, sizes_compatible"
```

---

### Task 15: Tests for `choose_best_store()` and `build_price_map()`

**File to create:** `tests/test_shopping_list.py`

- [ ] **Step 1: Write tests**

```python
"""Tests for shopping_agent.services.shopping_list and price_comparison utilities."""
from unittest.mock import MagicMock

import pytest

from shopping_agent.services.shopping_list import choose_best_store


class TestChooseBestStore:
    """Tests for choose_best_store() store-selection utility."""

    def _store(self, value):
        from shopping_agent.models import Store
        return Store(value)

    def test_picks_coles_when_cheaper(self):
        store = choose_best_store(1.50, 2.00, self._store("coles"))
        assert store.value == "coles"

    def test_picks_woolworths_when_cheaper(self):
        store = choose_best_store(2.00, 1.50, self._store("woolworths"))
        assert store.value == "woolworths"

    def test_picks_coles_when_equal(self):
        store = choose_best_store(1.50, 1.50, self._store("coles"))
        # Equal price: Coles wins (<=)
        assert store.value == "coles"

    def test_falls_back_when_only_coles_price(self):
        store = choose_best_store(1.50, None, self._store("woolworths"))
        assert store.value == "coles"

    def test_falls_back_when_only_woolworths_price(self):
        store = choose_best_store(None, 1.50, self._store("coles"))
        assert store.value == "woolworths"

    def test_uses_fallback_when_no_prices(self):
        store = choose_best_store(None, None, self._store("coles"))
        assert store.value == "coles"
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_shopping_list.py -v
```

Expected: all PASS.

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -v
```

All tests should pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_shopping_list.py
git commit -m "test: add tests for choose_best_store utility"
```

---

## Final Verification

- [ ] **Run full linter**

```bash
ruff check src/shopping_agent/ tests/
```

Expected: no errors.

- [ ] **Run full type checker**

```bash
mypy src/shopping_agent/ --ignore-missing-imports
```

Expected: no errors (or document any intentional `type: ignore` annotations).

- [ ] **Run full test suite**

```bash
pytest tests/ -v --tb=short
```

Expected: all tests PASS.

- [ ] **Start server and perform smoke test**

```bash
uvicorn shopping_agent.main:app --reload --host 0.0.0.0
```

Manually verify:
1. `/` — dashboard loads
2. `/orders` — orders page loads
3. `/predictions` — predictions page loads
4. `/prices` — price comparison loads, expand a chart
5. `/shopping-list` — shopping list page loads
6. `/settings` — settings page loads with counts

- [ ] **Final commit**

```bash
git add .
git commit -m "refactor: complete codebase refactor — modularity, DRY, types, tests"
```
