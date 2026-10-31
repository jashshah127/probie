"""Serve Muse Glimmer 30B on one Modal GPU, behind vLLM's OpenAI-compatible API.

This is the self-hosted rung of the ladder: invoice data goes to a GPU you control, and cost
is GPU time rather than tokens. It needs Modal 1.0 or later.

    pip install modal && modal setup
    modal secret create probie-glimmer API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
    modal deploy deploy/modal_glimmer.py

The deploy prints a URL like https://YOUR-WORKSPACE--probie-glimmer-serve.modal.run. In
probie.toml, uncomment the glimmer-modal model, set base_url to that URL plus /v1, and put the
same API_KEY in .env as MODAL_GLIMMER_KEY. Then:

    python -m probie ping --model glimmer-modal --tools

The first start downloads about 60 GB of weights into a Modal volume, so it takes a while; later
starts reuse them. The container shuts down after 15 idle minutes, and that idle time is billed
by Modal but not counted by Probie's run timer.

Serving flags follow the vLLM recipe for meta-models/Muse-Glimmer-30B: Glimmer writes its
reasoning and tool calls in its own channel format, so it needs vLLM 0.28 or later with the
muse_glimmer tool-call and reasoning parsers. Reasoning strength is a chat-template setting
(chat_template_kwargs.reasoning_strength); reasoning_effort does nothing on vLLM.
"""

import os
import subprocess

import modal

MODEL = "meta-models/Muse-Glimmer-30B"
SERVED_NAME = "muse-glimmer"
VLLM_VERSION = "0.28.0"
GPU = os.environ.get("GLIMMER_GPU", "H100")  # 80 GB fits the BF16 weights on one GPU
MAX_MODEL_LEN = int(os.environ.get("GLIMMER_MAX_MODEL_LEN", "32768"))  # plenty for one invoice and its records

image = (
    modal.Image.from_registry("nvidia/cuda:12.8.1-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .pip_install(f"vllm=={VLLM_VERSION}", "huggingface_hub[hf_transfer]")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)

app = modal.App("probie-glimmer")
hf_cache = modal.Volume.from_name("probie-hf-cache", create_if_missing=True)
vllm_cache = modal.Volume.from_name("probie-vllm-cache", create_if_missing=True)


@app.function(
    image=image,
    gpu=GPU,
    scaledown_window=15 * 60,
    timeout=60 * 60,
    volumes={"/root/.cache/huggingface": hf_cache, "/root/.cache/vllm": vllm_cache},
    secrets=[modal.Secret.from_name("probie-glimmer")],
)
@modal.concurrent(max_inputs=32)
@modal.web_server(port=8000, startup_timeout=40 * 60)
def serve():
    command = [
        "vllm", "serve", MODEL,
        "--served-model-name", SERVED_NAME,
        "--host", "0.0.0.0", "--port", "8000",
        "--api-key", os.environ["API_KEY"],
        "--max-model-len", str(MAX_MODEL_LEN),
        "--gpu-memory-utilization", "0.92",
        "--enable-prefix-caching",
        "--enable-auto-tool-choice", "--tool-call-parser", "muse_glimmer",
        "--reasoning-parser", "muse_glimmer",
        "--generation-config", "auto",
    ]
    subprocess.Popen(command)
