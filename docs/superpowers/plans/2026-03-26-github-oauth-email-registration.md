# GitHub OAuth + Email Registration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add GitHub OAuth and email registration to the login flow, using a dedicated `/auth/callback` page for session establishment.

**Architecture:** Two new FastAPI view routes (`/register`, `/auth/callback`) follow the exact same pattern as the existing `/login` view — each is a thin module rendering a Jinja2 template with Supabase config. All auth logic runs client-side via the Supabase JS SDK; the backend (`auth.py`, `api_auth.py`) requires no changes.

**Tech Stack:** FastAPI, Jinja2, Supabase JS SDK v2 (CDN), Tailwind CSS (CDN), pytest + pytest-asyncio

---

## File Map

| Action | File | Purpose |
|--------|------|---------|
| Create | `src/shopping_agent/routes/views/auth_callback.py` | View serving `GET /auth/callback` |
| Create | `src/shopping_agent/routes/views/register.py` | View serving `GET /register` |
| Create | `src/shopping_agent/templates/auth_callback.html` | Extracts session from URL hash, POSTs to `/api/auth/session`, redirects to `/` |
| Create | `src/shopping_agent/templates/register.html` | Email + password sign-up form with GitHub OAuth button |
| Modify | `src/shopping_agent/templates/login.html` | Add GitHub OAuth button and "Register" link |
| Modify | `src/shopping_agent/routes/views/__init__.py` | Import and register the two new routers |
| Modify | `tests/test_views_and_mcp.py` | Add tests for the two new views |

---

## Task 1: `/auth/callback` view and template

**Files:**
- Create: `src/shopping_agent/routes/views/auth_callback.py`
- Create: `src/shopping_agent/templates/auth_callback.html`
- Modify: `tests/test_views_and_mcp.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_views_and_mcp.py`:

```python
from shopping_agent.routes.views import auth_callback as auth_callback_view

@pytest.mark.asyncio
async def test_auth_callback_page_renders_with_supabase_config(monkeypatch, dummy_templates, make_request):
    monkeypatch.setattr(auth_callback_view, "templates", dummy_templates)
    monkeypatch.setattr(auth_callback_view, "settings", SimpleNamespace(
        supabase_url="https://test.supabase.co",
        supabase_anon_key="test-anon-key",
    ))
    request = make_request("/auth/callback")
    response = await auth_callback_view.auth_callback_page(request)
    assert len(dummy_templates.template_calls) == 1
    name, ctx = dummy_templates.template_calls[0]
    assert name == "auth_callback.html"
    assert ctx["supabase_url"] == "https://test.supabase.co"
    assert ctx["supabase_anon_key"] == "test-anon-key"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_views_and_mcp.py::test_auth_callback_page_renders_with_supabase_config -v
```

Expected: ImportError or AttributeError — `auth_callback` module does not exist yet.

- [ ] **Step 3: Create the view module**

Create `src/shopping_agent/routes/views/auth_callback.py`:

```python
"""Auth callback view — handles OAuth and email confirmation redirects."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...config import settings
from ...templating import templates

router = APIRouter()


@router.get("/auth/callback")
async def auth_callback_page(request: Request) -> HTMLResponse:
    """Render the OAuth/email-confirmation callback page."""
    return templates.TemplateResponse(
        request,
        "auth_callback.html",
        {
            "supabase_url": settings.supabase_url or "",
            "supabase_anon_key": settings.supabase_anon_key or "",
        },
    )
```

- [ ] **Step 4: Create the template**

Create `src/shopping_agent/templates/auth_callback.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Signing in… — Shopping Agent</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
</head>
<body class="bg-gray-50 min-h-screen flex items-center justify-center">
    <div class="text-gray-500 text-sm">Signing in…</div>

    <script>
        const SUPABASE_URL = "{{ supabase_url }}";
        const SUPABASE_ANON_KEY = "{{ supabase_anon_key }}";
        const sb = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

        (async () => {
            const { data, error } = await sb.auth.getSession();
            if (error || !data.session) {
                window.location.href = '/login?error=auth_failed';
                return;
            }
            const resp = await fetch('/api/auth/session', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ access_token: data.session.access_token }),
            });
            if (!resp.ok) {
                window.location.href = '/login?error=session_failed';
                return;
            }
            window.location.href = '/';
        })();
    </script>
</body>
</html>
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_views_and_mcp.py::test_auth_callback_page_renders_with_supabase_config -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/shopping_agent/routes/views/auth_callback.py src/shopping_agent/templates/auth_callback.html tests/test_views_and_mcp.py
git commit -m "feat: add /auth/callback view and template"
```

---

## Task 2: `/register` view and template

**Files:**
- Create: `src/shopping_agent/routes/views/register.py`
- Create: `src/shopping_agent/templates/register.html`
- Modify: `tests/test_views_and_mcp.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_views_and_mcp.py`:

```python
from shopping_agent.routes.views import register as register_view

@pytest.mark.asyncio
async def test_register_page_renders_with_supabase_config(monkeypatch, dummy_templates, make_request):
    monkeypatch.setattr(register_view, "templates", dummy_templates)
    monkeypatch.setattr(register_view, "settings", SimpleNamespace(
        supabase_url="https://test.supabase.co",
        supabase_anon_key="test-anon-key",
    ))
    request = make_request("/register")
    response = await register_view.register_page(request)
    assert len(dummy_templates.template_calls) == 1
    name, ctx = dummy_templates.template_calls[0]
    assert name == "register.html"
    assert ctx["supabase_url"] == "https://test.supabase.co"
    assert ctx["supabase_anon_key"] == "test-anon-key"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_views_and_mcp.py::test_register_page_renders_with_supabase_config -v
```

Expected: ImportError — `register` module does not exist yet.

- [ ] **Step 3: Create the view module**

Create `src/shopping_agent/routes/views/register.py`:

```python
"""Registration page view."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...config import settings
from ...templating import templates

router = APIRouter()


@router.get("/register")
async def register_page(request: Request) -> HTMLResponse:
    """Render the registration page (no auth required)."""
    return templates.TemplateResponse(
        request,
        "register.html",
        {
            "supabase_url": settings.supabase_url or "",
            "supabase_anon_key": settings.supabase_anon_key or "",
        },
    )
```

- [ ] **Step 4: Create the template**

Create `src/shopping_agent/templates/register.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Create Account — Shopping Agent</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
</head>
<body class="bg-gray-50 min-h-screen flex items-center justify-center">
    <div class="max-w-md w-full mx-4">
        <div class="bg-white shadow-md rounded-lg p-8">
            <h1 class="text-2xl font-bold text-gray-900 mb-6 text-center">Create Account</h1>
            <div id="error-msg" class="hidden mb-4 p-3 bg-red-50 border border-red-200 text-red-700 text-sm rounded"></div>
            <div id="success-msg" class="hidden mb-4 p-3 bg-green-50 border border-green-200 text-green-700 text-sm rounded">
                Check your email for a confirmation link.
            </div>

            <div id="form-section">
                <button id="github-btn" type="button"
                        class="w-full flex items-center justify-center gap-2 border border-gray-300 rounded-md px-4 py-2 text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 transition-colors mb-4">
                    <svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/>
                    </svg>
                    Sign up with GitHub
                </button>

                <div class="relative my-4">
                    <div class="absolute inset-0 flex items-center">
                        <div class="w-full border-t border-gray-300"></div>
                    </div>
                    <div class="relative flex justify-center text-sm">
                        <span class="bg-white px-2 text-gray-500">or</span>
                    </div>
                </div>

                <form id="register-form" class="space-y-4">
                    <div>
                        <label for="email" class="block text-sm font-medium text-gray-700 mb-1">Email</label>
                        <input type="email" id="email" name="email" required
                               class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                    </div>
                    <div>
                        <label for="password" class="block text-sm font-medium text-gray-700 mb-1">Password</label>
                        <input type="password" id="password" name="password" required minlength="8"
                               class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                    </div>
                    <button type="submit" id="submit-btn"
                            class="w-full bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 px-4 rounded-md text-sm transition-colors">
                        Create account
                    </button>
                </form>

                <p class="mt-4 text-center text-sm text-gray-600">
                    Already have an account?
                    <a href="/login" class="text-blue-600 hover:underline">Sign in</a>
                </p>
            </div>
        </div>
    </div>

    <script>
        const SUPABASE_URL = "{{ supabase_url }}";
        const SUPABASE_ANON_KEY = "{{ supabase_anon_key }}";
        const sb = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

        const form = document.getElementById('register-form');
        const errorDiv = document.getElementById('error-msg');
        const successDiv = document.getElementById('success-msg');
        const formSection = document.getElementById('form-section');
        const submitBtn = document.getElementById('submit-btn');
        const githubBtn = document.getElementById('github-btn');

        githubBtn.addEventListener('click', async () => {
            githubBtn.disabled = true;
            githubBtn.textContent = 'Redirecting…';
            await sb.auth.signInWithOAuth({
                provider: 'github',
                options: { redirectTo: window.location.origin + '/auth/callback' },
            });
        });

        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            errorDiv.classList.add('hidden');
            submitBtn.disabled = true;
            submitBtn.textContent = 'Creating account…';

            const email = document.getElementById('email').value;
            const password = document.getElementById('password').value;

            const { error } = await sb.auth.signUp({ email, password });
            if (error) {
                errorDiv.textContent = error.message;
                errorDiv.classList.remove('hidden');
                submitBtn.disabled = false;
                submitBtn.textContent = 'Create account';
                return;
            }

            formSection.classList.add('hidden');
            successDiv.classList.remove('hidden');
        });
    </script>
</body>
</html>
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_views_and_mcp.py::test_register_page_renders_with_supabase_config -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/shopping_agent/routes/views/register.py src/shopping_agent/templates/register.html tests/test_views_and_mcp.py
git commit -m "feat: add /register view and template with GitHub OAuth and email sign-up"
```

---

## Task 3: Update login.html with GitHub button and register link

**Files:**
- Modify: `src/shopping_agent/templates/login.html`

No unit test required — this is a template-only change. Manual verification is sufficient.

- [ ] **Step 1: Update `login.html`**

Replace the current contents of `src/shopping_agent/templates/login.html` with:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign In — Shopping Agent</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
</head>
<body class="bg-gray-50 min-h-screen flex items-center justify-center">
    <div class="max-w-md w-full mx-4">
        <div class="bg-white shadow-md rounded-lg p-8">
            <h1 class="text-2xl font-bold text-gray-900 mb-6 text-center">Shopping Agent</h1>
            <div id="error-msg" class="hidden mb-4 p-3 bg-red-50 border border-red-200 text-red-700 text-sm rounded"></div>

            <button id="github-btn" type="button"
                    class="w-full flex items-center justify-center gap-2 border border-gray-300 rounded-md px-4 py-2 text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 transition-colors mb-4">
                <svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/>
                </svg>
                Sign in with GitHub
            </button>

            <div class="relative my-4">
                <div class="absolute inset-0 flex items-center">
                    <div class="w-full border-t border-gray-300"></div>
                </div>
                <div class="relative flex justify-center text-sm">
                    <span class="bg-white px-2 text-gray-500">or</span>
                </div>
            </div>

            <form id="login-form" class="space-y-4">
                <div>
                    <label for="email" class="block text-sm font-medium text-gray-700 mb-1">Email</label>
                    <input type="email" id="email" name="email" required
                           class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                <div>
                    <label for="password" class="block text-sm font-medium text-gray-700 mb-1">Password</label>
                    <input type="password" id="password" name="password" required
                           class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                <button type="submit" id="submit-btn"
                        class="w-full bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 px-4 rounded-md text-sm transition-colors">
                    Sign in
                </button>
            </form>

            <p class="mt-4 text-center text-sm text-gray-600">
                Don't have an account?
                <a href="/register" class="text-blue-600 hover:underline">Create one</a>
            </p>
        </div>
    </div>

    <script>
        const SUPABASE_URL = "{{ supabase_url }}";
        const SUPABASE_ANON_KEY = "{{ supabase_anon_key }}";
        const sb = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

        const form = document.getElementById('login-form');
        const errorDiv = document.getElementById('error-msg');
        const submitBtn = document.getElementById('submit-btn');
        const githubBtn = document.getElementById('github-btn');

        // Show error from callback redirect if present
        const urlError = new URLSearchParams(window.location.search).get('error');
        if (urlError) {
            errorDiv.textContent = urlError === 'auth_failed' ? 'Authentication failed. Please try again.' : 'Failed to establish session.';
            errorDiv.classList.remove('hidden');
        }

        githubBtn.addEventListener('click', async () => {
            githubBtn.disabled = true;
            githubBtn.textContent = 'Redirecting…';
            await sb.auth.signInWithOAuth({
                provider: 'github',
                options: { redirectTo: window.location.origin + '/auth/callback' },
            });
        });

        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            errorDiv.classList.add('hidden');
            submitBtn.disabled = true;
            submitBtn.textContent = 'Signing in...';

            const email = document.getElementById('email').value;
            const password = document.getElementById('password').value;

            const { data, error } = await sb.auth.signInWithPassword({ email, password });
            if (error) {
                errorDiv.textContent = error.message;
                errorDiv.classList.remove('hidden');
                submitBtn.disabled = false;
                submitBtn.textContent = 'Sign in';
                return;
            }

            const resp = await fetch('/api/auth/session', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ access_token: data.session.access_token }),
            });
            if (!resp.ok) {
                errorDiv.textContent = 'Failed to establish session.';
                errorDiv.classList.remove('hidden');
                submitBtn.disabled = false;
                submitBtn.textContent = 'Sign in';
                return;
            }
            window.location.href = '/';
        });
    </script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add src/shopping_agent/templates/login.html
git commit -m "feat: add GitHub OAuth button and register link to login page"
```

---

## Task 4: Register new routers

**Files:**
- Modify: `src/shopping_agent/routes/views/__init__.py`

- [ ] **Step 1: Update `routes/views/__init__.py`**

Replace the current contents of `src/shopping_agent/routes/views/__init__.py` with:

```python
"""View routes package — one module per page domain."""
from fastapi import APIRouter

from .auth_callback import router as auth_callback_router
from .dashboard import router as dashboard_router
from .health import router as health_router
from .login import router as login_router
from .orders import router as orders_router
from .predictions import router as predictions_router
from .prices import router as prices_router
from .product_lookup import router as product_lookup_router
from .register import router as register_router
from .settings import router as settings_router
from .shopping_list import router as shopping_list_router

router = APIRouter()
router.include_router(health_router)
router.include_router(login_router)
router.include_router(register_router)
router.include_router(auth_callback_router)
router.include_router(dashboard_router)
router.include_router(orders_router)
router.include_router(predictions_router)
router.include_router(prices_router)
router.include_router(product_lookup_router)
router.include_router(shopping_list_router)
router.include_router(settings_router)
```

- [ ] **Step 2: Run the full test suite**

```bash
pytest -v
```

Expected: all tests pass (including the two new ones from Tasks 1 and 2).

- [ ] **Step 3: Commit**

```bash
git add src/shopping_agent/routes/views/__init__.py
git commit -m "feat: register /register and /auth/callback routers"
```

---

## Supabase Dashboard Checklist (manual, one-time)

Before testing end-to-end:

- [ ] Authentication → Providers → GitHub: enable, enter GitHub OAuth App Client ID and Secret
- [ ] GitHub OAuth App → Authorization callback URL: set to `https://<project>.supabase.co/auth/v1/callback`
- [ ] Authentication → URL Configuration → Redirect URLs: add `<base_url>/auth/callback`
- [ ] Authentication → Email Templates → Confirm signup: ensure redirect URL points to `<base_url>/auth/callback`
