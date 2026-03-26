# Auth Expansion: GitHub OAuth + Email Registration

**Date:** 2026-03-26
**Status:** Approved

## Overview

Extend the existing Supabase-based auth to support two new flows:
- Sign in / sign up with GitHub OAuth
- Email + password registration with email confirmation

The existing JWT verification and cookie session mechanism (`auth.py`, `api_auth.py`) requires no changes — Supabase tokens are identical regardless of how the user authenticated.

## New Files

| File | Purpose |
|------|---------|
| `src/shopping_agent/routes/views/register.py` | View serving `GET /register` |
| `src/shopping_agent/routes/views/auth_callback.py` | View serving `GET /auth/callback` |
| `src/shopping_agent/templates/register.html` | Sign-up form with GitHub button |
| `src/shopping_agent/templates/auth_callback.html` | Minimal callback handler page |

## Modified Files

| File | Change |
|------|--------|
| `src/shopping_agent/templates/login.html` | Add GitHub button + "Register" link |
| `src/shopping_agent/routes/views/__init__.py` | Register the two new routers |
| `.env.example` | No changes needed — no new env vars required |

## Auth Flows

### GitHub OAuth

1. User clicks "Sign in with GitHub" on `/login` or `/register`
2. JS calls `sb.auth.signInWithOAuth({ provider: 'github', options: { redirectTo: '<base_url>/auth/callback' } })`
3. Browser redirects: our app → GitHub → Supabase → `/auth/callback#access_token=...`
4. `/auth/callback` page calls `sb.auth.getSession()`, POSTs the access token to `/api/auth/session`, redirects to `/`

### Email Registration

1. User visits `/register`, submits email + password
2. JS calls `sb.auth.signUp({ email, password })`
3. On success: form is hidden, "Check your email for a confirmation link" message shown
4. User clicks confirmation link in email → Supabase redirects to `/auth/callback`
5. Same callback flow as GitHub OAuth

### Email Sign-in (unchanged)

Existing flow unchanged. GitHub button and "Don't have an account? Register" link added to the login page.

## Configuration

No new application env vars. The Supabase dashboard requires two one-time changes:

1. **GitHub OAuth provider** — Authentication → Providers → GitHub: enable, enter GitHub OAuth App Client ID and Secret. The GitHub OAuth App's "Authorization callback URL" must be set to `https://<project>.supabase.co/auth/v1/callback`.

2. **Redirect URL allowlist** — Authentication → URL Configuration → Redirect URLs: add `<base_url>/auth/callback` (e.g. `https://localhost:8000/auth/callback`). Required for both GitHub OAuth and email confirmation redirects.

## Template Design

**`register.html`** mirrors `login.html` in structure:
- Email + password fields
- "Sign up" submit button
- GitHub OAuth button (same as login page)
- "Already have an account? Sign in" link
- Post-submit state: hide form, show confirmation message

**`auth_callback.html`** is minimal:
- Blank/loading page
- JS calls `sb.auth.getSession()` on load
- On session: POST to `/api/auth/session`, redirect to `/`
- On error: redirect to `/login` with an error query param

**`login.html`** additions:
- GitHub OAuth button above or below the form divider
- "Don't have an account? Register" link below the form

## Non-goals

- No server-side OAuth code exchange (Supabase JS SDK handles this entirely)
- No additional env vars (GitHub secret lives in Supabase dashboard only)
- No changes to JWT verification logic
- No password reset flow (out of scope)
