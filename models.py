"""Model catalog for /model and effort levels for /effort.

Speeds and tool support were measured against integrate.api.nvidia.com on 2026-10-05;
the shared endpoint's load changes, so treat them as rough.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    blurb: str          # what it is good at
    tools: bool         # called tools in testing (needed for code graph / Sentry)
    effort: str         # "thinking": enable_thinking + reasoning_budget; "reasoning_effort": low/medium/high


MODELS = [
    ModelInfo("nvidia/nemotron-3-ultra-550b-a55b", "Nemotron 3 Ultra",
              "Most capable · deep cross-stack debugging · slow (~17 tok/s), often busy",
              True, "thinking"),
    ModelInfo("nvidia/nemotron-3-super-120b-a12b", "Nemotron 3 Super",
              "Best everyday pick · strong and fast (~115 tok/s) · reliable tools",
              True, "thinking"),
    ModelInfo("poolside/laguna-xs-2.1", "Laguna XS 2.1",
              "Coding-agent model · very fast (~124 tok/s) · quick code traces",
              True, "thinking"),
    ModelInfo("openai/gpt-oss-20b", "GPT-OSS 20B",
              "OpenAI open model · fast (~60 tok/s) · /effort matters most here",
              True, "reasoning_effort"),
    ModelInfo("nvidia/nemotron-3.5-lightning-30b-a3b", "Nemotron 3.5 Lightning",
              "Small and light · quick questions, summaries (~28 tok/s)",
              True, "thinking"),
    ModelInfo("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "Nemotron 3 Nano Omni",
              "Small reasoning model · text, image, audio input (~31 tok/s)",
              True, "thinking"),
    ModelInfo("moonshotai/kimi-k3", "Kimi K3",
              "Long-context writing and analysis · very slow (~3 tok/s)",
              False, "thinking"),
    ModelInfo("z-ai/glm-5.3", "GLM 5.3",
              "Strong reasoning and coding · extremely slow (~1 tok/s) now",
              True, "thinking"),
    ModelInfo("deepseek-ai/deepseek-v4.1-flash", "DeepSeek V4.1 Flash",
              "Fast general model · was busy (timed out) in testing",
              False, "thinking"),
    ModelInfo("google/gemma-4-31b-it", "Gemma 4 31B",
              "Google open model · general chat · was busy in testing",
              False, "thinking"),
]

BY_ID = {m.id: m for m in MODELS}

# name -> (description, enable_thinking, reasoning_budget tokens or None, reasoning_effort)
EFFORTS = {
    "off": ("No thinking · fastest answers (gpt-oss still thinks briefly)", False, None, "low"),
    "low": ("Brief thinking · quick checks and simple lookups", True, 1024, "low"),
    "medium": ("Balanced · most questions and normal debugging", True, 4096, "medium"),
    "high": ("Thinks as long as it needs · hard bugs and cross-stack investigations", True, None, "high"),
}
DEFAULT_EFFORT = "medium"


def effort_params(model_id, effort):
    """Request kwargs for an effort level. Templates ignore chat_template_kwargs they
    don't know, so those are always sent; reasoning_effort only goes to models known
    to take it (others may 400 on it)."""
    _, thinking, budget, level = EFFORTS[effort]
    kwargs = {"enable_thinking": thinking}
    if thinking and budget:
        kwargs["reasoning_budget"] = budget
    params = {"extra_body": {"chat_template_kwargs": kwargs}}
    info = BY_ID.get(model_id)
    if info and info.effort == "reasoning_effort":
        params["reasoning_effort"] = level
    return params
