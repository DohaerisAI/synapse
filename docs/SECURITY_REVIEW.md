# Synapse Security Review Report

**Date:** 2026-03-07
**Reviewer:** Claude Opus 4.6 (security-reviewer agent)
**Scope:** Full codebase review (~8K LOC, 50 modules)

## Summary

Reviewed 11 target files plus supporting modules. Found **3 CRITICAL**, **5 HIGH**, **4 MEDIUM**, and **4 LOW/INFO** findings. No hardcoded secrets were found. All SQL is parameterized with one structural note. The biggest risks are unrestricted SSRF, unauthenticated API surface, unvalidated file-path upload, and command execution without sandboxing.

---

## CRITICAL Findings

### CRIT-1: Unauthenticated API -- All Endpoints Accessible Without Auth

**Files:** `synapse/app.py` (all routes)

Every FastAPI route has zero authentication middleware. Anyone who can reach the HTTP port can inject arbitrary messages, approve pending actions, read full memory snapshots, and read auth credential metadata.

**Fix:** Add API key or bearer-token middleware before any sensitive route.

---

### CRIT-2: Unrestricted SSRF -- `web.fetch` Accepts Any User-Supplied URL

**Files:** `synapse/executors.py:219-228`, `synapse/gateway/planner.py:67`

The `web.fetch` action passes a completely unchecked URL directly to `httpx.AsyncClient.get()`. This enables requests to cloud metadata endpoints, internal services, etc.

**Fix:** Validate URL scheme (https only) and hostname against a blocklist of internal/private ranges.

---

### CRIT-3: Shell Execution Without Sandboxing

**Files:** `synapse/executors.py:200-218`, `synapse/broker.py:40`

`IsolatedExecutor` calls `asyncio.create_subprocess_exec` directly on the host -- no Docker container, no namespace isolation. The broker labels it "docker" but no containerization exists.

**Fix (short-term):** Disable `shell.exec` in broker until real sandbox exists.
**Fix (long-term):** Run commands inside Docker with `--network none`, read-only mounts, time/memory limits.

---

## HIGH Findings

### HIGH-1: Path Traversal -- `gws.drive.upload` Accepts Arbitrary Filesystem Paths

**File:** `synapse/gws.py:296-312`

Upload path from user/AI payload has no restriction. Could upload `/etc/shadow`, `~/.ssh/id_rsa`, etc.

**Fix:** Resolve path and assert it sits under a configured workspace root.

---

### HIGH-2: `gws.exec` -- Arbitrary GWS CLI Command Execution

**Files:** `synapse/gws.py:399-405`, `synapse/broker.py:62-76`

The approval check pattern-matches `send/delete/trash/remove` but novel destructive operations bypass it.

**Fix:** Require unconditional approval for all `gws.exec` calls.

---

### HIGH-3: Telegram Webhook -- No Signature Verification

**File:** `synapse/app.py:120-136`

The endpoint does not verify the `X-Telegram-Bot-Api-Secret-Token` header. Any party who discovers the webhook URL can inject forged messages.

**Fix:** Verify the secret token header against a configured value.

---

### HIGH-4: Dynamic Plugin Execution Without Path Validation

**File:** `synapse/plugins/loader.py:33-43`

The `entry_point` field from plugin manifest is loaded without path confinement. No signature check, no hash verification.

**Fix:** Restrict plugin loading to subdirectories of a controlled `plugins/` directory.

---

### HIGH-5: Approval Endpoint Has No Session/Ownership Check

**File:** `synapse/app.py:138-147`

Any unauthenticated party who knows an approval ID can approve pending actions.

**Fix:** Resolved by CRIT-1 (API key auth) + store `user_id` in approval record.

---

## MEDIUM Findings

### MED-1: SQL F-String for Dynamic IN Clause

**File:** `synapse/store.py:207-212, 671-676`

The f-string only interpolates `?, ?, ?` pattern (not user data) -- safe today but fragile.

**Fix:** Add invariant comments and assert enum membership.

---

### MED-2: Auth `health_view` Exposes Internal File Paths

**Files:** `synapse/auth.py:119-155`, `synapse/app.py:47`

`GET /api/auth` returns absolute paths to credential files.

**Fix:** Strip absolute paths from public API response.

---

### MED-3: User Query Embedded in `codex exec` Argument

**File:** `synapse/executors.py:153-192`

User search query passed verbatim to `codex` binary. Timeout is 180 seconds.

**Fix:** Sanitize/truncate query, reduce timeout to 30-60 seconds.

---

### MED-4: GWS Drive Search -- Insufficient Escaping

**File:** `synapse/gws.py:286-293`

Only single quotes escaped; Drive API query injection possible.

**Fix:** Use structured query builder or proper escaping per Google Drive API spec.

---

## LOW / INFO Findings

### LOW-1: Telegram Token in URLs (Log Exposure)

**File:** `synapse/adapters.py:60,126`

Bot token embedded in request URLs. Leaks to logs and DB via error messages.

### LOW-2: HTML Rendering Order Creates Escaping Fragility

**File:** `synapse/adapters.py:75-77`

Code block stashing before HTML escape creates ordering dependency.

### LOW-3: Global Memory Path Traversal -- Mitigated

**File:** `synapse/memory.py:65-66`

`safe_component` properly strips unsafe characters. Working as intended.

### INFO-1: No Security-Focused Dependencies

**File:** `pyproject.toml`

No JWT library, no rate limiting, no secret encryption. Recommend `pip-audit` before deploying.

---

## Priority Remediation Order

1. CRIT-1 -- Add API key auth middleware
2. CRIT-3 -- Disable `shell.exec` until real sandbox
3. CRIT-2 -- Add SSRF blocklist for `web.fetch`
4. HIGH-3 -- Telegram webhook secret verification
5. HIGH-1 -- Path confinement for `gws.drive.upload`
6. HIGH-2 -- Unconditional approval for `gws.exec`
7. HIGH-4 -- Confine plugin entry points
8. MED-2 -- Redact file paths from `/api/auth`
9. LOW-1 -- Redact bot token from error messages

## Status

These are known issues to be addressed incrementally. The security findings do NOT block the initial git commit of the existing codebase -- they represent the current state and will be tracked as future work.
