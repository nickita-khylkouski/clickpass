# Blunt Facts Report (With Code + Sources)

Date: 2026-03-01

## Fact 1: Crabwalk is older than Clawtrace (public timestamps)

- Crabwalk tags existed before Feb 7:
  - `v1.0.1` (2026-01-26)
  - `v1.0.11` (2026-02-05)
  - Source: https://github.com/luccast/crabwalk/tags
- Clawtrace first commit is Feb 28:
  - Source: https://github.com/dibbaa-code/clawtrace/commit/2b063e0bd428bdf5fdfb915b9fda6c771b6e0df5

This is a direct timeline mismatch with “clawtrace came first.”

---

## Fact 2: Early Clawtrace exactly matches 3 Crabwalk files from Feb 19 commit

Compared:
- Clawtrace: `43c8d2e291e8242895ce8303d91cc2797bd37b02`
- Crabwalk: `ea99ca93fd36b4aa2991a0b1401974bb0e881c15` (2026-02-19)

Exact SHA-256 matches:

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

Full-file diff counts:

```text
device_diff_lines=0
router_diff_lines=0
monitor_diff_lines=0
```

Sources:
- Crabwalk commit: https://github.com/luccast/crabwalk/commit/ea99ca93fd36b4aa2991a0b1401974bb0e881c15
- Clawtrace tree: https://github.com/dibbaa-code/clawtrace/tree/43c8d2e291e8242895ce8303d91cc2797bd37b02

---

## Fact 3: One of those exact-match files did not exist in Crabwalk Feb 5 tag

Checked `crabwalk@v1.0.11`:

```text
v1.0.11_has_device_ts=no
```

File:
- `src/integrations/openclaw/device.ts`

Sources:
- Crabwalk v1.0.11 commit: https://github.com/luccast/crabwalk/commit/6ca27b1e81c92a5cf5cd5548056f9c32548bb664
- Crabwalk Feb 19 commit adding file: https://github.com/luccast/crabwalk/commit/ea99ca93fd36b4aa2991a0b1401974bb0e881c15

---

## Fact 4: Code excerpts are verbatim identical across repos

### Exhibit 4A — `device.ts` excerpt (identical)

Code shown below appears in both:
- Clawtrace source: https://github.com/dibbaa-code/clawtrace/blob/43c8d2e291e8242895ce8303d91cc2797bd37b02/src/integrations/openclaw/device.ts
- Crabwalk source: https://github.com/luccast/crabwalk/blob/ea99ca93fd36b4aa2991a0b1401974bb0e881c15/src/integrations/openclaw/device.ts

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

### Exhibit 4B — `trpc/router.ts` excerpt (identical)

Sources:
- Clawtrace: https://github.com/dibbaa-code/clawtrace/blob/43c8d2e291e8242895ce8303d91cc2797bd37b02/src/integrations/trpc/router.ts
- Crabwalk: https://github.com/luccast/crabwalk/blob/ea99ca93fd36b4aa2991a0b1401974bb0e881c15/src/integrations/trpc/router.ts

```ts
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

### Exhibit 4C — `routes/monitor/index.tsx` excerpt (identical)

Sources:
- Clawtrace: https://github.com/dibbaa-code/clawtrace/blob/43c8d2e291e8242895ce8303d91cc2797bd37b02/src/routes/monitor/index.tsx
- Crabwalk: https://github.com/luccast/crabwalk/blob/ea99ca93fd36b4aa2991a0b1401974bb0e881c15/src/routes/monitor/index.tsx

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

---

## Fact 5: LICENSE header is the same in both repos

Sources:
- Clawtrace LICENSE: https://github.com/dibbaa-code/clawtrace/blob/main/LICENSE
- Crabwalk LICENSE: https://github.com/luccast/crabwalk/blob/main/LICENSE

Code/text from both files:

```text
MIT License

Copyright (c) 2026 Luciano Castillo Vega
```

---

## Fact 6: README intro/feature framing is clearly derived

Sources:
- Clawtrace README: https://github.com/dibbaa-code/clawtrace/blob/main/README.md
- Crabwalk README: https://github.com/luccast/crabwalk/blob/main/README.md

Clawtrace top block:

```md
# 🦀 Clawtrace
Real-time companion monitor for [OpenClaw (Clawdbot)](...) ...
Watch your AI agents work across WhatsApp, Telegram, Discord, and Slack in a live node graph.
```

Crabwalk top block:

```md
# 🦀 Crabwalk
Real-time companion monitor for [OpenClaw (Clawdbot)](...) ...
Watch your AI agents work across WhatsApp, Telegram, Discord, and Slack in a live node graph.
```

---

## Fact 7: Feb 7 contribution signal exists on both accounts

Source endpoints:
- https://github.com/users/srilaasya/contributions?from=2026-02-01&to=2026-02-28
- https://github.com/users/dibbaa-code/contributions?from=2026-02-01&to=2026-02-28

Observed output lines:

```text
srilaasya: >7 contributions on February 7
srilaasya: >5 contributions on February 28
srilaasya: >13 contributions on March 1

dibbaa-code: >10 contributions on February 7
dibbaa-code: >19 contributions on February 28
dibbaa-code: >10 contributions on March 1
```

This proves activity counts, not private repo contents.

---

## Blunt conclusion

- Public chronology, exact hashes, zero-diff files, and verbatim excerpts support this: `clawtrace` substantially reuses `crabwalk` code.
- Reverse direction (“crabwalk copied clawtrace”) is not supported by these public facts.
- If your hackathon required from-scratch event coding or strict prior-code disclosure, this is strong evidence for a formal integrity challenge.

