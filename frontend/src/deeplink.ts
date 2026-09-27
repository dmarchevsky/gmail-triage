// Deep links that survive Cloudflare Access re-authentication.
//
// Routes live in the URL fragment (HashRouter), but the fragment is lost when
// an expired Access session bounces the browser through the Access/Google login
// pages. A query parameter survives that round trip, so links that may start a
// login (notification clicks, re-auth reloads) use /?open=<route>, which is
// rewritten to /#<route> before the router mounts.

const OPEN_PARAM = "open";

/** Rewrite /?open=/some/route into /#/some/route (call before rendering). */
export function applyOpenParam(): void {
  const params = new URLSearchParams(window.location.search);
  const open = params.get(OPEN_PARAM);
  if (open === null) return;
  params.delete(OPEN_PARAM);
  const route = open.startsWith("/") ? open : "/";
  const rest = params.toString();
  window.history.replaceState(
    null,
    "",
    window.location.pathname + (rest ? `?${rest}` : "") + "#" + route,
  );
}

/** The current in-app route, e.g. "/digests/runs/5". */
export function currentRoute(): string {
  return window.location.hash.replace(/^#/, "") || "/";
}

/** A login-safe URL for an in-app route. */
export function openUrl(route: string): string {
  return route === "/" ? "/" : `/?${OPEN_PARAM}=${encodeURIComponent(route)}`;
}
