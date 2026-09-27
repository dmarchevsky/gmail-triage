# Remote access — Cloudflare Tunnel + Access

MailTriage has no password of its own. It is published through a `cloudflared` tunnel
and sits behind a Cloudflare Access application that signs you in with Google. The app
then checks the result itself.

```
browser ──https──▶ Cloudflare edge ──Access: Google sign-in──▶ tunnel ──▶ cloudflared ──http──▶ mailtriage:8080
                                     (adds Cf-Access-Jwt-Assertion)
```

- **The tunnel is outbound only.** `cloudflared` dials out to Cloudflare, so nothing on
  the host is exposed to the internet. The app port is published on `127.0.0.1:8080` only.
- **Access decides who reaches the origin.** Access lets through only the Google accounts
  named in its policy, and adds a signed JWT to every request it forwards.
- **The app checks that JWT itself.** Every `/api/` request needs a
  `Cf-Access-Jwt-Assertion` header that passes these checks:
  - the signature verifies against the team's keys at `https://<team>/cdn-cgi/access/certs`;
  - the AUD and issuer match, and the token hasn't expired;
  - the `email` claim is in `CF_ACCESS_ALLOWED_EMAILS`.

  If Access is ever misconfigured, the app still refuses the request.
- **The only exempt paths** are `/api/v1/status`, the docker healthcheck, and
  `/api/v1/auth/session`, which tells the UI why a visitor was refused.

This mirrors chore-tracker's setup. If you already run that, you **reuse the same Zero Trust
team and the same Google login method**. There's nothing new to set up on Google's side for
sign-in.

## Setup

### 1. Create the tunnel

1. In the Zero Trust dashboard, go to **Networks → Tunnels → Create a tunnel → Cloudflared**.
2. Name it `mailtriage`.
3. Copy the token (the long string after `cloudflared service install`).

### 2. Add the public hostname

In the tunnel config, go to **Published application routes → Add**:

| Field | Value |
|---|---|
| Subdomain / domain | `mail` / `example.com` |
| Type | `HTTP` |
| URL | `mailtriage:8080` |

`mailtriage` is the compose service name. `cloudflared` reaches it over the compose network,
**not** via the host's loopback port.

### 3. Google login method

This step is skipped if the team already has one, e.g. from chore-tracker.

1. In Google Cloud, create an OAuth client of type **Web application**.
2. Set its redirect URI to exactly `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`.
3. In Zero Trust, go to **Settings → Authentication → Login methods → Add new → Google** and
   paste the client ID and secret.
4. Disable **One-time PIN**, so that Google is the only way in.

This client is separate from the Gmail-API client that MailTriage uses for your mailbox
(step 6).

### 4. The Access application

Go to **Access → Applications → Add an application → Self-hosted**:

| Field | Value |
|---|---|
| Name | `MailTriage` |
| Domain | `mail.example.com`, path empty (the whole hostname) |
| Identity providers | **Google only** (turn *Accept all available identity providers* off) |
| Instant Auth | On |
| Session duration | your choice (e.g. `24 hours` or `1 week`) |
| Cookie settings | **SameSite: Lax**, **HTTP Only: on** |

Add a policy named `Owner`: Action **Allow**, Include → **Emails** → your Google address.

Why the cookie settings matter: the app has no cookie of its own, so the Access cookie's
`SameSite=Lax` is what keeps other sites from making requests as you. Keep it at **Lax**,
not *Strict*: Google's redirect back after connecting Gmail is a cross-site navigation, and
it must still carry the cookie.

Then open the application's **Overview** and copy its **Application Audience (AUD) tag**.

### 5. `.env` on the host

```sh
COMPOSE_PROFILES=tunnel
CLOUDFLARE_TUNNEL_TOKEN=<token from step 1>
CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
CF_ACCESS_AUD=<AUD tag from step 4>
CF_ACCESS_ALLOWED_EMAILS=you@gmail.com
# CF_ACCESS_ISSUER=https://<original-team-name>.cloudflareaccess.com   # only if the team was renamed
```

Remove `DEV_AUTH` and the old `UI_PASSWORD`. `DEV_AUTH` together with any `CF_ACCESS_*`
value makes the app refuse to start. `UI_PASSWORD` is no longer read.

Then run `docker compose up -d --build`. The log should show `cf_access_jwks_ready` and
`startup_complete`.

**If sign-in fails with "Invalid issuer"**, the team was renamed at some point. Cloudflare
keeps the original name in `iss`. The rejection line in the log shows both values:

```sh
docker compose logs mailtriage | grep cf_access_rejected | tail -1
```

Copy the `token_iss` value from that line into `CF_ACCESS_ISSUER`.

### 6. Gmail OAuth client (the mailbox connection)

1. In the Google Cloud Console, go to **Credentials** and open the Gmail OAuth client.
2. Add this under Authorized redirect URIs:
   `https://mail.example.com/api/v1/gmail/oauth/callback`
3. In MailTriage, set **Settings → Notifications → Public base URL** to
   `https://mail.example.com`.

The redirect URI the app sends to Google is built from that setting. Without it, the URI
would come out as `http://…`, because TLS ends at Cloudflare, and Google would reject it.

The existing Gmail connection keeps working through the switch. Its refresh token isn't tied
to a redirect URI, so you only need the new one when you click **Connect** again.

### 7. Install as an app (PWA) and push notifications

MailTriage ships a web app manifest and a service worker, so it can be installed from the
browser: Chrome/Edge → *Install app*, Android → *Add to Home screen*, iPhone/iPad (Safari)
→ Share → *Add to Home Screen*.

**Let the icons and manifest through Access (recommended).** Browsers fetch the manifest
and icons outside the page, sometimes without cookies (Android re-checks the manifest in
the background to update the installed app). Behind Access, those fetches get the login
redirect and the install or update fails. The page already asks for the manifest with
credentials, which covers the usual case. To make install and updates reliable, add a second
Access application that bypasses these static, non-sensitive files:

| Field | Value |
|---|---|
| Name | `MailTriage static` |
| Domain | `mail.example.com`, paths `manifest.webmanifest`, `icons/*`, `favicon.svg` |
| Policy | Action **Bypass**, Include → **Everyone** |

Everything else (the UI, `sw.js`, `/api/*`) stays behind the Google-only application.

**Session duration.** An installed app is opened and closed often, and each expired session
means another Google sign-in. Use a longer session on the main application (e.g. `1 month`).

**What re-auth looks like.** When the Access session expires, the app's next API call is
redirected to the Access login, and the app reloads itself through it. In an installed app the
Google page opens as an in-app sheet and returns to the app afterwards. The app keeps its
current screen across the sign-in: it reloads as `/?open=<route>`, because the `#/route`
fragment doesn't survive the Access → Google → Access redirects. iOS keeps a separate
cookie jar for Home-Screen apps, so you sign in once inside the installed app even if Safari
is already signed in.

**Push notifications.** Turn on *In-app* delivery on a digest (Digests → Configure → edit),
then on each device open **Settings → Notifications → Notifications on this device → Turn on**.
Each in-app digest appears as unread in **Digests → Inbox** and is pushed to those devices.
Tapping the notification opens the digest, signing in first if the session expired.

- Pushes go from the server straight to the browser vendor's push service (Google FCM,
  Mozilla, Apple, Microsoft). They never pass through the tunnel or Access. The container
  needs outbound HTTPS to them, and the server only sends to those hosts.
- On iPhone/iPad, push works only in the Home-Screen app (iOS 16.4+), not in a Safari tab.
- The VAPID key is generated on first use and stored encrypted in the settings table. Purging
  configuration (Settings → Data) resets it, so turn notifications on again on each device.

## Break-glass (Cloudflare or Google down)

On the host:

1. Stop the tunnel: `docker compose stop cloudflared`.
2. In `.env`, comment out every `CF_ACCESS_*` line and set `DEV_AUTH=true`.
3. Run `docker compose up -d`.
4. From your machine, run `ssh -L 8080:127.0.0.1:8080 <host>` and open
   http://localhost:8080.

Undo all of this afterwards. `DEV_AUTH` means **no authentication at all**, which is only
tolerable because the port is loopback-only and the tunnel is stopped.

## Rotating the tunnel token

1. In the dashboard, open the tunnel and choose **Refresh token**.
2. Update `CLOUDFLARE_TUNNEL_TOKEN` in `.env`.
3. Run `docker compose up -d`.

## Verify

```sh
H=https://mail.example.com
curl -s -o /dev/null -w '%{http_code}\n' $H/api/v1/settings   # 302 → <team>.cloudflareaccess.com

# on the host, straight to the origin (bypassing Access):
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/v1/status        # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/v1/settings      # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'Cf-Access-Jwt-Assertion: junk' \
     http://127.0.0.1:8080/api/v1/settings                                         # 401
```

- **Browser, private window:** open `$H`. You should get Google sign-in and then the
  dashboard, with no app password prompt.
- **A Google account that isn't on the policy:** turned away at the edge. An account that
  is on the policy but not in `CF_ACCESS_ALLOWED_EMAILS` gets an app page that names the
  address and offers to switch account.
- **Settings → Security → Sign out:** lands on the Access logout page. Revisiting `$H`
  asks for Google again.
- **Settings → Mailbox → Reconnect:** Google consent, then back to the app showing
  "Gmail connected". This confirms the redirect URI from step 6.
- **External port scan of the host:** `8080` is not reachable from other machines.
