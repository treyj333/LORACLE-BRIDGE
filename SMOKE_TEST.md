# LORACLE BRIDGE — Equal-Citizens Smoke Test

Hand-run checklist for the Phase 1–4 + cross-protocol DM work. Nothing here
has been exercised on real MT + MC hardware yet; this is the fastest path
to catching what fake state + unit tests missed.

Print it, run through it, cross off lines. Expected outcomes are quoted. If
an expected outcome doesn't hold, capture logs + screenshots and we'll fix
before building the next feature on top.

## Setup

Before starting:

- [ ] `./mesh-llm.sh` starts cleanly on your dev machine
- [ ] Browser open to `http://localhost:8000`
- [ ] Both radios physically connected (one MT, one MC)
- [ ] A second "remote" MT radio available (phone app or second device)
- [ ] A second "remote" MC radio available

Leave the Chrome DevTools Console open — any red errors are a bug, even if
the UI looks fine.

---

## 1. Dual-radio connect + unified tree

- [ ] First-run wizard — step 1 reads **"STEP 1 OF 2 — PRIMARY RADIO"** and
      **"CONNECT YOUR FIRST RADIO"** (not "MESHTASTIC")
- [ ] Connect MT radio, success panel shows **"MESHTASTIC CONNECTED"** in
      teal with the circle dot
- [ ] Advance to step 2 — reads **"STEP 2 OF 2 — SECOND RADIO (OPTIONAL)"**
- [ ] Connect MC radio, success panel shows **"MESHCORE CONNECTED"** in
      purple with the diamond dot
- [ ] Back on the main canvas: both **MY MT** and **MY MC** self-nodes are
      visible at the centre, side-by-side
- [ ] Node sidebar (☰ button) lists peers from *both* meshes with
      colour-coded `mt` (teal) and `mc` (purple) badges on every row

**If any peer only appears after sending traffic from it, that's the
MC-ingest fix from 2026-04-18 regressing.** Flag for fix.

## 2. Protocol scope selector

Top-bar selector reads `[ ALL | MT | MC ]`. Expected: filter is
render-time-only; switching doesn't rebuild the mesh or drop sockets.

- [ ] Default scope is `ALL`; all peers + both self-nodes visible
- [ ] Click **MT** — MC peers and the MY MC self-node disappear from canvas,
      sidebar, and map; scope button glows teal
- [ ] Click **MC** — inverse; scope button glows purple
- [ ] Reload the page with scope still on `MC` — it persists across reloads
      (localStorage-backed)
- [ ] Back to `ALL`, verify the node set is identical to before the filter

## 3. MeshCore-first connect flow

Disconnect all radios (CONFIG → DISCONNECT or restart bridge). Reopen the
first-run wizard.

- [ ] In step 1, change **PROTOCOL** dropdown to **MeshCore**
- [ ] Enter MC transport + address, click CONNECT
- [ ] Success panel shows **"MESHCORE CONNECTED"** in purple (not MT teal)
- [ ] Paragraph below says "add a Meshtastic radio" as the optional second
      step (flipped direction from the default flow)
- [ ] Proceed past step 2 by skipping — ends up on the main canvas with MC
      running as the only backend, no fake `mt-primary` self-node

## 4. Cross-protocol DM from the dashboard (local operator path)

Already worked before any of this refactor; sanity-check it still does.

- [ ] Click an MC peer in the node tree — float window opens
- [ ] Type a message, send — recipient actually receives it on their MC radio
- [ ] Reply comes back, shows up in the same float window
- [ ] Repeat with an MT peer in a second float window — both windows stay
      open (no close-on-open mutex)

## 5. Cross-protocol DM from remote users (`!dm` command)

This is the new feature from commit `c7809a4`. Make sure you've enabled it:

- [ ] CONFIG tab → **CROSS-PROTOCOL DM** section → checkbox **ON**
- [ ] Toast says "Cross-protocol DM enabled"

Now from the *remote* MT radio (phone/second device), not the bridge host:

- [ ] Send DM to the bridge's MT node: `!dm <mc-nickname> hi from MT`
- [ ] Bridge replies: `Sent DM to mc:... on meshcore.`
- [ ] The remote MC peer whose nickname you used actually receives a DM on
      their MC radio, prefixed `from meshtastic (<your-short-name>): hi from MT`
- [ ] Turn the flag OFF in CONFIG
- [ ] Repeat the `!dm` from the remote MT node — bridge replies:
      `!dm: cross-protocol DM is disabled on this bridge.`
- [ ] The remote MC peer receives *nothing* (blocked, not silently dropped
      to the logs)

Edge cases:

- [ ] `!dm ghost hello` — bridge replies "no contact named 'ghost'"
- [ ] Two contacts with the same nickname on the other mesh —
      `!dm <ambiguous> hi` returns a candidate list, doesn't send
- [ ] `!dm mc:<concrete-id> hi` — bypasses the nickname lookup, delivers

## 6. BRIDGE tab — per-direction stats + coloured arrows

With public-channel relay enabled (`AUTO-BRIDGE PUBLIC CHANNEL` on):

- [ ] Send a ch-0 message from remote MT to the bridge — it relays to MC
- [ ] BRIDGE tab `MT → MC` column `relayed` increments; column is teal
- [ ] Send a ch-0 message from remote MC — relays to MT
- [ ] `MC → MT` column `relayed` increments; column is purple
- [ ] Flow log entries show `mt→mc` in teal and `mc→mt` in purple
- [ ] Send a duplicate of a recent message — `dropped` count goes up in the
      appropriate direction column (dedup cache catches the loop)

## 7. Feature parity — traceroute + radio config

Expected: MT operations work, MC returns a clean 501 toast.

- [ ] Open an MT peer's float window, click TRACEROUTE — works as before
- [ ] Open an MC peer's float window, click TRACEROUTE — toast reads
      roughly "meshcore: traceroute not supported by this radio"
      (not a silent failure, not a crash)
- [ ] CONFIG tab with MT as primary radio — REGION, MODEM, TX POWER, HOPS
      fields populate and SAVE works
- [ ] Disconnect MT, run MC-only — CONFIG tab shows a read-only notice
      ("This radio (MC) exposes a read-only config") and SAVE is disabled

## 8. Scope does not block sends

Scope is a visibility filter, not an access control. Confirm the API still
honours out-of-scope targets when you know the id.

- [ ] Scope to `MT`
- [ ] In the browser console: `callApi('POST', '/api/threads/mc:<known-id>/send', {text: 'smoke'})`
- [ ] Message is delivered on the MC side (sender saw the button, they
      know what they're doing)

## 9. No regressions

- [ ] Console has **zero** red errors across all the steps above
- [ ] `./venv/bin/python -m pytest -q` from `meshtastic-bridge/` shows
      `315 passed`

---

## Capturing findings

For anything that fails, capture:

1. Which step number
2. What you saw vs what was expected (one sentence)
3. Screenshot of the UI state (incl. DevTools Console panel if relevant)
4. The most recent ~50 log lines from the bridge terminal

Open an issue or paste it back to the chat and we'll fix before moving on.
