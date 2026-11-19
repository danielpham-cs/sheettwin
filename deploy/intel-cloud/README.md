# SheetTwin on the hackathon VM (Intel Cloud, 2x Arc Pro B70)

```
Browser ──https──► JupyterLab /6/ ──jupyter-server-proxy──► 127.0.0.1:8765  sheettwin (FastAPI + UI)
                                                                  │ OpenAI API
                                                                  ▼
                                                    127.0.0.1:8000  llm (vLLM 0.33 XPU, Qwen3.6-35B-A3B-FP8, TP=2)
```

**Open the app:** take the address JupyterLab opens at from the console's *Connect* button
(`https://<jupyter-host>/<n>/lab`) and replace `lab` with `proxy/8765/app/`. Anyone opening it must be signed in to Intel
Cloud with access to the reservation. The UI picks up the prefix per request (`X-Forwarded-Prefix`), so nothing needs
rebuilding if `<jupyter-host>` or `<n>` change.

**Each user has their own data.** The gateway signs people in and passes their e-mail (`Oidc_claim_email`) through the
proxy; with `SHEETIR_USER_HEADER` set (see the compose file), every request works in `/data/users/<e-mail>/`, so
workbooks, projects and project documents are never shared between users. `GET /api/me` says who you are. A request
without the header (e.g. `curl` on the VM itself) uses the shared `/data/` as before. The LLM call log
(`/data/.logs/llm.jsonl`) stays shared, on the VM only.

## Start / stop
```bash
cd ~/sheettwin/deploy/intel-cloud
docker compose up -d --build        # first start ~5 min: vLLM compiles and captures XPU graphs
docker compose ps                   # llm must be "healthy" before sheettwin starts
docker compose logs -f llm          # or: logs -f sheettwin
docker compose down                 # stop everything (data stays in volume sheettwin_sheettwin-data)
```
Both services restart by themselves (`restart: unless-stopped`). The chat falls back to the built-in rules while the
model is down, so the UI never breaks during a restart.

## On a new VM
1. `git clone` the repo, check out this branch.
2. `hf download Qwen/Qwen3.6-35B-A3B-FP8` (37.5 GB, ~15 min).
3. Restore the data backup (see `backup.sh`), then `docker compose up -d --build`.

## Measured here (quotation_sample.xlsx, 3 questions)
| Backend | Cards | Decode | Answers |
|---|---|---|---|
| vLLM 0.26 FP8, eager | 2 | 3.5 tok/s | 78–115 s |
| llama.cpp SYCL, GGUF Q5_K_M | 1 | 91 tok/s | 4.8–8.9 s |
| **vLLM 0.33 FP8 + XPU graph (this stack)** | 2 | 86 tok/s | **2.3–6.2 s** |
| vLLM AWQ int4 | – | fails: MoE kernel error on XPU (0.26 and 0.33) | – |

Single-card fallback (frees a card, ~2–3 s slower per answer): run
`ghcr.io/ggml-org/llama.cpp:server-intel -m <Qwen3.6-35B-A3B-UD-Q5_K_M.gguf> -ngl 99 --jinja -c 65536 -np 4 --port 8000`
with `ONEAPI_DEVICE_SELECTOR=level_zero:0` instead of the `llm` service.

## Rules of the VM
- Never bind port **8888**: it is JupyterLab, the only way in.
- The reservation ends **2026-12-02 11:23** (hard 30-day limit). Run `./backup.sh` and download the archive before then.
