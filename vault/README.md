# Autonomous Trader — Memory Vault

Open this folder as a vault in Obsidian (File → Open folder as vault). It is the agent's persistent memory: the trading pipeline writes it, the nightly reflection distills it, and every morning cycle reads it back before proposing trades.

- **Journal/** — one note per trading day: every cycle's proposals and outcomes, plus the nightly reflection.
- **Positions/** — one note per symbol: the standing thesis (why it's held, what would trigger an exit) and a full action history.
- **Lessons.md** — rules distilled from realized wins and losses. Injected into every trading prompt; capped at 30 so only current, earned lessons survive.
- **Scorecard.md** — rolling hit rate of each signal source vs 5-day forward returns, and the blend weights derived from it (retuned Mondays).
- **Newsletters/** — the nightly email, archived.

Everything here is derived from the Supabase audit trail — the vault is the distilled memory, never the only record.
