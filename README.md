# Connecting Your BQE CORE Account

This tool reads your firm's time & expense data **directly from BQE CORE's API** to build a complete historical archive — bypassing CORE's "Export as CSV", which silently truncates large pulls.

Because of how CORE's API works, **each firm registers its own free app with BQE** and runs this tool with that app's ID. Your CORE data is granted only to *your* app and flows straight into your own machine — it never passes through anyone else's server, and stays private.

This is a one-time setup, about 5–10 minutes. You'll do three things:

1. Create a free CORE developer account 
2. Register an app (read-only) and copy its **Client ID**
3. Paste the Client ID into the tool

> The developer account is **separate from your normal CORE login** — that's expected and correct. You only use it to register the app. You'll still authorize the tool by signing in with your regular CORE company credentials.

---

## Part 1 — Create your developer account (~2 min)

1. Go to BQE's API docs at **https://api-explorer.bqecore.com** and follow the **Developer Portal** link, then click **Sign Up → Get Started**. https://api-developer.bqecore.com/webapp/Account/Welcome
2. Enter your name, email, and a password. **Any email works** — it does *not* have to be the email you use to log in to CORE.
3. Read and agree to the **Developer Agreement**, then click **Sign Up**.
4. BQE emails you an **activation link**. Click it to finish setting up the account.

Creating the developer account and apps is **free**. (The app can only read data from a CORE company that has an active paid subscription — which your firm already has.)

---

## Part 2 — Register the app (~3 min)

1. Log in to the Developer Portal. On the **Dashboard**, click **Add New Application**. https://api-developer.bqecore.com/webapp/Account/Login
2. Fill in the fields using **exactly these settings**:

| Setting | Value | Why it matters |
|---|---|---|
| **Application Name** | anything, e.g. `CORE Archive Builder` | just a label you'll recognize |
| **Application Type** | **Native App** | a public/PKCE type with **no Client Secret** — safe to run locally and to share, unlike a "Regular Web App" |
| **Redirect URI** | `http://127.0.0.1:8080/callback` | must match the tool **character-for-character** (see warning below) |
| **Post Logout Redirect URI** | `http://127.0.0.1:8080` | must match the tool's **base URL** (see warning below) |
| **Description** | anything | — |

3. Click **Create**.
4. Open the app's details and **copy the Client ID** — you'll need it in Part 3.

Because **Native App** is a public type, there is **no Client Secret** to copy. That's correct — don't go looking for one.

> ⚠️ **Register the Redirect URI exactly as `http://127.0.0.1:8080/callback`.** CORE matches it precisely: `http` vs `https`, `127.0.0.1` vs `localhost`, a trailing slash, the port number, and capitalization all count as different. If it doesn't match what the tool sends, you'll get an error page instead of a login screen.

The tool requests two permission **scopes** when you connect (you don't set these at registration, but it's good to know what you'll see):

- **`read:core`** — read-only access. The tool can never change anything in CORE.
- **`offline_access`** — lets it refresh quietly so you're not logging in on every run.

---

## Part 3 — Put your Client ID into the tool

1. Open the tool and go to **Settings → Connect** _(TODO: confirm exact location/field name once the connect UI is built)_.
2. Paste your **Client ID** into the **CORE Client ID** field.
3. Click **Connect to CORE**.
4. You'll see **CORE's own login screen** — sign in with your normal CORE credentials, pick your company if you have more than one, and click **Grant Permission**.

That's it. The tool pulls your complete T&E history through the API and you're ready to build the archive.

---

## What the consent screen asks for

When you click Connect, CORE shows a consent screen naming your app and the access it wants:

- **Read-only access to company data** — the tool reads; it cannot edit or delete anything.
- **Offline access** — so it can refresh its session without making you log in every time.

Granting read-only is deliberately the lightest thing to approve — the tool never needs write access.

---

## Troubleshooting

- **Error page right after you log in / "redirect_uri mismatch"** → the Redirect URI you registered doesn't exactly match the tool's. Recheck `http` vs `https`, `127.0.0.1` vs `localhost`, the trailing slash, and the port.
- **`invalid_scope` error** → the tool must request a basic scope (`read:core`); `offline_access` on its own isn't enough.
- **"App can only interact with companies with a valid subscription"** → expected; the CORE company needs an active paid subscription.
- **It worked, then stopped after about an hour** → access tokens last ~1 hour. The refresh token from *offline access* renews it automatically; if you declined offline access, just connect again.

---

## Maintainer notes (repo owner only — remove or move before publishing)

- This walkthrough assumes the tool registers a **Native App**, uses redirect `http://127.0.0.1:8080/callback`, and requests `read:core` + `offline_access`. **Keep these three in sync with the tool's actual connect code.** If you change the port or callback path in the tool, update the registered value in Part 2 to match.
- **Do not ship your own Client ID as a default.** The whole point of the bring-your-own-app model is that each firm's authorizations attach to *their* app, not yours. The Client ID field must be empty/required, with a placeholder only.
- The redirect mechanism is the one piece that depends on how the tool is run (a small local listener on `127.0.0.1:8080`, as in BQE's Python/Node samples). Confirm the final value here once that flow exists.
