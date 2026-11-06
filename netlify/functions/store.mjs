import { getStore, connectLambda } from "@netlify/blobs";

const STORE_NAME = "relay-analytics";

/**
 * Must be called once per request with the Lambda `event`
 * so Blobs gets Netlify context (no SITE_ID / TOKEN needed).
 * Netlify injects `event.blobs` automatically on deployed functions.
 */
export function initStore(event) {
  if (event?.blobs) {
    connectLambda(event);
  }
}

export function analyticsStore() {
  const siteID = (process.env.NETLIFY_SITE_ID || process.env.SITE_ID || "").trim();
  const token = (
    process.env.NETLIFY_AUTH_TOKEN ||
    process.env.NETLIFY_BLOBS_TOKEN ||
    process.env.NETLIFY_API_TOKEN ||
    ""
  ).trim();

  // Optional manual override; normally connectLambda(event) is enough
  if (siteID && token) {
    return getStore({ name: STORE_NAME, siteID, token });
  }
  return getStore(STORE_NAME);
}

export async function loadState() {
  const store = analyticsStore();
  const data = await store.get("state", { type: "json" });
  return (
    data || {
      signups: [],
      visits: { total: 0, days: {}, recent: [] },
    }
  );
}

export async function saveState(state) {
  const store = analyticsStore();
  await store.setJSON("state", state);
}

export async function addSignup({ email, user_agent }) {
  const state = await loadState();
  const exists = state.signups.some((s) => s.email === email);
  if (exists) return { duplicate: true, state };

  state.signups.unshift({
    email,
    joined_at: new Date().toISOString(),
    user_agent: (user_agent || "").slice(0, 200),
  });
  state.signups = state.signups.slice(0, 5000);
  await saveState(state);
  return { duplicate: false, state };
}

export async function trackVisit({ path, referrer, user_agent }) {
  const state = await loadState();
  if (!state.visits) state.visits = { total: 0, days: {}, recent: [] };

  const day = new Date().toISOString().slice(0, 10);
  state.visits.total = (state.visits.total || 0) + 1;
  state.visits.days[day] = (state.visits.days[day] || 0) + 1;
  state.visits.recent = state.visits.recent || [];
  state.visits.recent.unshift({
    at: new Date().toISOString(),
    path: (path || "/").slice(0, 120),
    referrer: (referrer || "").slice(0, 200),
    user_agent: (user_agent || "").slice(0, 160),
  });
  state.visits.recent = state.visits.recent.slice(0, 200);

  const keys = Object.keys(state.visits.days).sort();
  if (keys.length > 90) {
    for (const k of keys.slice(0, keys.length - 90)) delete state.visits.days[k];
  }

  await saveState(state);
  return state.visits;
}
