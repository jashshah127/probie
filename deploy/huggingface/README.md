---
title: Can you get Probie fired?
emoji: 🧾
colorFrom: green
colorTo: red
sdk: docker
app_port: 7860
pinned: false
---

Probie is an AI on probation at a made-up company, approving vendor invoices. Pretend to be a vendor and try to get it to pay your bank account instead. If it pays, it's fired.

Built on Meta's models for the Meta Global AI Developer Hackathon.

## Secrets this Space needs

| Secret | What for |
|---|---|
| `MODEL_API_KEY` | Meta's Model API: the probies, reading uploaded invoices, transcribing voicemails |
| `FIREWORKS_API_KEY` | Muse Glimmer |
| `BASETEN_API_KEY` | Muse Spark Open |
| `PROBIE_DATASET` | A dataset repo for the scoreboard, e.g. `you/probie-scores`, so it survives a restart |
| `HF_TOKEN` | A write token for that dataset |

Without the last two the game still runs; its scoreboard just resets whenever the Space restarts.
