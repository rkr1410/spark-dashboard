# vLLM Runtime Configs

Executable notes for local model serving on HAL-9000. The `.env` files are the
source of truth for model-specific flags; this directory can also be opened from
Obsidian if plain notes are useful.

## Commands

Start GPT-OSS:

```sh
docker compose --env-file runtimes/gptoss.env -f runtimes/compose.vllm.yml up -d
```

Start VibeThinker:

```sh
docker compose --env-file runtimes/vibethinker.env -f runtimes/compose.vllm.yml up -d
```

Start Gemma 4 26B:

```sh
docker compose --env-file runtimes/gemma4-26b.env -f runtimes/compose.vllm.yml up -d
```

Start DiffusionGemma:

```sh
docker compose --env-file runtimes/diffusiongemma.env -f runtimes/compose.vllm.yml up -d
```

Each env file sets its own `COMPOSE_PROJECT_NAME`, so these can run side by side
as long as their container names and ports are different.

Stop the selected runtime:

```sh
docker compose --env-file runtimes/gptoss.env -f runtimes/compose.vllm.yml down
docker compose --env-file runtimes/vibethinker.env -f runtimes/compose.vllm.yml down
docker compose --env-file runtimes/gemma4-26b.env -f runtimes/compose.vllm.yml down
docker compose --env-file runtimes/diffusiongemma.env -f runtimes/compose.vllm.yml down
```

Watch logs:

```sh
docker logs -f gptoss
docker logs -f vibethinker
docker logs -f gemma4-26b
docker logs -f diffusiongemma
```

Check the API:

```sh
curl http://127.0.0.1:8000/v1/models
curl http://127.0.0.1:8001/v1/models
curl http://127.0.0.1:8002/v1/models
curl http://127.0.0.1:8003/v1/models
```

If an old stopped container reserves the name:

```sh
docker rm gptoss
docker rm vibethinker
docker rm gemma4-26b
docker rm diffusiongemma
```

## Current Runtimes

| Runtime | Model | Port | GPU mem | Context | Notes |
| --- | --- | ---: | ---: | ---: | --- |
| `gptoss` | `openai/gpt-oss-120b` | 8000 | 0.665 | 131072 | `ctx_fit` measured as 1.71x; uses GPT-OSS reasoning/tool parsers. |
| `vibethinker` | `WeiboAI/VibeThinker-3B` | 8001 | 0.15 | 32768 | Plain vLLM chat serving; no parser flags by default. |
| `gemma4-26b` | `google/gemma-4-26b-a4b-it` | 8002 | 0.5 | 131072 | Plain transformer Gemma 4 profile using the Gemma vLLM image. |
| `diffusiongemma` | `nvidia/diffusiongemma-26B-A4B-it-NVFP4` | 8003 | 0.5 | 262144 | Separate NVIDIA diffusion checkpoint with Gemma 4 parsers and thinking enabled. |

## Parser Notes

GPT-OSS uses explicit parser flags:

```text
--tool-call-parser openai --reasoning-parser openai_gptoss --enable-auto-tool-choice
```

VibeThinker is intentionally kept without parser flags. Its Hugging Face model
card says it was not trained for tool-calling or agent-based programming data.
The model is also not listed in vLLM's built-in reasoning parser table; it is a
Qwen2.5-derived reasoning model, not Qwen3, so `--reasoning-parser qwen3` would
be an assumption rather than a supported default.

If VibeThinker later emits stable reasoning delimiters that should be separated
from final answers, add that as a deliberate experiment in a new branch/env file.

Gemma 4 and DiffusionGemma both use Gemma parser names in vLLM:

```text
--tool-call-parser gemma4 --reasoning-parser gemma4 --enable-auto-tool-choice
```

The plain Gemma profile is the transformer model. DiffusionGemma is a separate
NVIDIA diffusion checkpoint; its model card additionally recommends
`--trust-remote-code`, `--max-num-seqs 4`, `--attention-backend TRITON_ATTN`, and
thinking enabled through chat-template kwargs.
