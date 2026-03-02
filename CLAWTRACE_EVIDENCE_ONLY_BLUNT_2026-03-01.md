# Evidence-Only Packet (Blunt)

Date: 2026-03-01

## Claim being tested
Did `dibbaa-code/clawtrace` substantially copy from `luccast/crabwalk`, and is reverse direction plausible?

---

## Exhibit A: Repo chronology (public)

- `crabwalk` had public tags before Feb 7:
  - `v1.0.1` at `2026-01-26T16:56:05-05:00`
  - `v1.0.11` at `2026-02-05T14:08:13-05:00`
  - Source: https://github.com/luccast/crabwalk/tags

- `clawtrace` first public commit:
  - `2b063e0` at `2026-02-28T12:00:00-08:00`
  - Source: https://github.com/dibbaa-code/clawtrace/commit/2b063e0bd428bdf5fdfb915b9fda6c771b6e0df5

This chronology supports `crabwalk -> clawtrace`.

---

## Exhibit B: Similarity metrics (src ts/tsx)

Measured:

```text
clawtrace main vs crabwalk main: shared=48 identical=29 changed=19
clawtrace@43c8d2e vs crabwalk main: shared=48 identical=41 changed=7
clawtrace@43c8d2e vs crabwalk v1.0.11: shared=47 identical=38 changed=9
```

Early clawtrace is closer to crabwalk than current clawtrace is.

---

## Exhibit C: Exact file-hash identity with post-Feb-7 crabwalk state

For these three files, `clawtrace@43c8d2e` hash == `crabwalk@ea99ca93` hash:

```text
src/integrations/openclaw/device.ts
clawtrace_43c8d2e=3457ca770239e21b994a89450b21f00423a80309bef81fdde620a7a32c25aa11
crabwalk_ea99ca93=3457ca770239e21b994a89450b21f00423a80309bef81fdde620a7a32c25aa11

src/integrations/trpc/router.ts
clawtrace_43c8d2e=be8c657fac2189e3f6d00ca63d5e88c5648a9edd8d00213c2f83fcf64671deb4
crabwalk_ea99ca93=be8c657fac2189e3f6d00ca63d5e88c5648a9edd8d00213c2f83fcf64671deb4

src/routes/monitor/index.tsx
clawtrace_43c8d2e=522b30b3ebcb790ae37cbc70d0faad5caed2e84e29871fe3436c19dae89e5ec7
crabwalk_ea99ca93=522b30b3ebcb790ae37cbc70d0faad5caed2e84e29871fe3436c19dae89e5ec7
```

Binary result of full-file diff for each:

```text
device_diff_lines=0
router_diff_lines=0
monitor_diff_lines=0
```

Link to commit with those file states:
- https://github.com/luccast/crabwalk/commit/ea99ca93fd36b4aa2991a0b1401974bb0e881c15

---

## Exhibit D: `ea99ca93` explicitly adds/modifies those files (public commit metadata)

```text
ea99ca93fd36b4aa2991a0b1401974bb0e881c15|2026-02-19T18:35:52Z|Jamie Taylor|Bugfix/openclaw device identity auth (#62)

A src/integrations/openclaw/device.ts
M src/integrations/trpc/router.ts
M src/routes/monitor/index.tsx
```

And check against Feb 5 tag:

```text
v1.0.11_has_device_ts=no
```

This means one exact-matching file in early clawtrace did not exist in crabwalk’s Feb 5 snapshot, but did exist in Feb 19 crabwalk commit.

---

## Exhibit E: Verbatim identical code snippets (same line content)

### `src/integrations/openclaw/device.ts` (first lines identical)

```ts
import fs from 'fs'
import path from 'path'
import {
  createHash,
  createPrivateKey,
  generateKeyPairSync,
  sign,
} from 'crypto'
import type { ConnectChallengePayload, ConnectDevice } from './protocol'

const DATA_DIR = path.join(process.cwd(), 'data')
const DEVICE_IDENTITY_FILE = path.join(DATA_DIR, 'device-identity.json')
```

Source links:
- Clawtrace: https://github.com/dibbaa-code/clawtrace/blob/43c8d2e291e8242895ce8303d91cc2797bd37b02/src/integrations/openclaw/device.ts
- Crabwalk: https://github.com/luccast/crabwalk/blob/ea99ca93fd36b4aa2991a0b1401974bb0e881c15/src/integrations/openclaw/device.ts

### `src/integrations/trpc/router.ts` identical block

```ts
// Persistence service
persistenceStatus: publicProcedure.query(() => {
  const persistence = getPersistenceService()
  return persistence.getStatus()
}),

persistenceStart: publicProcedure.mutation(() => {
  const persistence = getPersistenceService()
  return persistence.start()
}),

persistenceStop: publicProcedure.mutation(() => {
  const persistence = getPersistenceService()
  return persistence.stop()
}),
```

Source links:
- Clawtrace: https://github.com/dibbaa-code/clawtrace/blob/43c8d2e291e8242895ce8303d91cc2797bd37b02/src/integrations/trpc/router.ts
- Crabwalk: https://github.com/luccast/crabwalk/blob/ea99ca93fd36b4aa2991a0b1401974bb0e881c15/src/integrations/trpc/router.ts

### `src/routes/monitor/index.tsx` identical block

```ts
const RETRY_DELAY = 3000
const MAX_RETRIES = 10
const DEFAULT_GATEWAY_ENDPOINT = 'ws://127.0.0.1:18789'
type AuthState = 'unknown' | 'authorized' | 'unpaired' | 'unauthorized' | 'degraded'

interface PairingState {
  requestId?: string
  message?: string
}
```

Source links:
- Clawtrace: https://github.com/dibbaa-code/clawtrace/blob/43c8d2e291e8242895ce8303d91cc2797bd37b02/src/routes/monitor/index.tsx
- Crabwalk: https://github.com/luccast/crabwalk/blob/ea99ca93fd36b4aa2991a0b1401974bb0e881c15/src/routes/monitor/index.tsx

---

## Exhibit F: Same copyright holder in both repos

Both LICENSE files state:

```text
MIT License

Copyright (c) 2026 Luciano Castillo Vega
```

Links:
- https://github.com/dibbaa-code/clawtrace/blob/main/LICENSE
- https://github.com/luccast/crabwalk/blob/main/LICENSE

---

## Exhibit G: Feb 7 contribution counts (account-level signal)

Public contribution endpoint outputs:

```text
srilaasya: >7 contributions on February 7
srilaasya: >5 contributions on February 28
srilaasya: >13 contributions on March 1

dibbaa-code: >10 contributions on February 7
dibbaa-code: >19 contributions on February 28
dibbaa-code: >10 contributions on March 1
```

Sources:
- https://github.com/users/srilaasya/contributions?from=2026-02-01&to=2026-02-28
- https://github.com/users/dibbaa-code/contributions?from=2026-02-01&to=2026-02-28

Important: contribution counts do not disclose which private repo they came from.

---

## Exhibit H: What clawtrace added (not denied)

New files in clawtrace not in crabwalk main:

```text
src/lib/threat-analyzer.ts (106 lines)
src/lib/alert-service.ts (54 lines)
src/components/effects/PixelWaves.tsx (114 lines)
src/components/effects/CrabTrails.tsx (43 lines)
src/components/effects/SandGradient.tsx (19 lines)
setup.sh (163 lines)
bin/clawtrace (297 lines)
```

This shows additions exist, but does not erase strong base-code overlap evidence.

---

## Blunt bottom line

- Public timeline + exact file hashes + zero-diff code blocks show substantial reuse from crabwalk into clawtrace.
- Reverse hypothesis (crabwalk copied from clawtrace) is not supported by the public chronology and file-state evidence.
- If hackathon rules required from-scratch event coding or mandatory disclosure of prior code, this evidence is enough for a serious integrity challenge.

