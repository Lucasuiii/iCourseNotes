"""LLM-based course lecture summarization with a versioned prompt file."""

import time
import json
from pathlib import Path

from openai import OpenAI

from src.runtime import config
from src.ai.tavily_enrichment import enrich_summary
from src.ai.asr_review import review_windows
from src.ai.course_glossary import course_terms, terminology_reference

_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "lecture_summary.md"
)


def load_system_prompt(path: str | Path | None = None) -> str:
    """Load the DeepSeek-compatible system prompt from Markdown.

    Keeping the prompt outside Python makes writing changes reviewable without
    touching provider or pipeline logic.  Missing or empty prompts fail closed
    instead of silently sending the model an ungoverned request.
    """
    prompt_path = Path(path) if path is not None else _DEFAULT_PROMPT_PATH
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"Summary prompt is empty: {prompt_path}")
    return prompt


class Summarizer:
    """Course lecture summarizer with multi-provider fallback.

    Iterates config.MODEL_PROVIDERS in declared order. Within each provider,
    tries each model in declared order. Returns the first successful result.
    Setting only DASHSCOPE_API_KEY still works because the default
    MODEL_PROVIDERS list ships a modelscope entry that reads it.
    """

    def __init__(self):
        self.system_prompt = load_system_prompt()
        self.providers = config.resolve_model_providers()
        if not self.providers:
            raise ValueError(
                "No model provider available. "
                "Set at least one provider's API key (e.g. DASHSCOPE_API_KEY)."
            )
        self._clients = {
            p["name"]: OpenAI(api_key=p["api_key"], base_url=p["base_url"])
            for p in self.providers
        }

    def _call_llm(self, client: OpenAI, model: str,
                  title: str, content: str) -> str:
        t0 = time.time()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"以下是课程《{title}》的原始材料。请沿能够确认"
                        "的授课脉络，将其整理为详细、连贯、适合复习的"
                        "课程笔记。\n\n"
                        f"<course_material>\n{content}\n</course_material>"
                        + terminology_reference(title)
                    ),
                },
            ],
            temperature=0.2,
            timeout=180,
        )
        if not response.choices:
            raise ValueError("API returned empty choices — likely content filter or quota exceeded")
        result = response.choices[0].message.content
        elapsed = time.time() - t0
        # Token usage helps explain run cost — every provider's billing is
        # token-based, and rate-limit decisions key off prompt size much
        # more than character count.  Some providers (OpenAI-compatible)
        # leave usage None on streaming or error paths, so fall back to a
        # plain "no usage" line so the summary still prints.
        usage = getattr(response, "usage", None)
        if usage is not None:
            print(
                f"[Summarizer] Done ({model}): "
                f"{len(content)} chars input → {len(result)} chars output"
                f" in {elapsed:.0f}s "
                f"(tokens: prompt={getattr(usage,'prompt_tokens','?')}, "
                f"completion={getattr(usage,'completion_tokens','?')})"
            )
        else:
            print(
                f"[Summarizer] Done ({model}): {len(content)} chars input"
                f" → {len(result)} chars output in {elapsed:.0f}s"
            )
        return result

    def summarize(self, title: str, content: str) -> tuple[str, str]:
        """Summarize lecture, trying providers in MODEL_PROVIDERS order.

        Returns (summary, model_used) where model_used is "{provider}/{model}".

        Raises:
            RuntimeError: if all providers/models fail.
        """
        if not content or not content.strip():
            return ("（内容为空）", "")

        errors = []
        for provider in self.providers:
            client = self._clients[provider["name"]]
            for model in provider["models"]:
                model_id = f"{provider['name']}/{model}"
                try:
                    result = self._call_llm(client, model, title, content)
                    result = enrich_summary(
                        result,
                        api_key=config.TAVILY_API_KEY,
                        client=client,
                        model=model,
                    )
                    return (result, model_id)
                except Exception as e:
                    print(f"[Summarizer] {model_id} failed: "
                          f"{type(e).__name__}")
                    errors.append(f"{model_id}: {e}")

        raise RuntimeError(
            "All LLM models failed:\n" + "\n".join(errors)
        )

    def find_unclear_windows(self, windows: list[dict], ppt_pages: list[dict],
                             excluded: set[tuple[int, int]],
                             *, course_title: str = "", terms: list[str] | None = None) -> list[dict]:
        """One optional review call, using the existing first provider/model."""
        provider = self.providers[0]
        try:
            return review_windows(
                self._clients[provider["name"]], provider["models"][0],
                windows, ppt_pages, excluded,
                disable_thinking=provider["name"] == "deepseek",
                terms=terms if terms is not None else course_terms(course_title),
            )
        except Exception as exc:
            print(f"[ASR review] Unavailable ({type(exc).__name__}); "
                  "keeping local transcription.")
            return []

    def homework_image_reader(self, ledger, checkpoint):
        """Use the explicit DeepSeek vision route, never text-only fallback."""
        if not any(p['name'] == 'deepseek' for p in self.providers):
            return None
        from src.ai.homework_vision import read_images
        client = self._clients['deepseek']
        return lambda frames: read_images(client, config.HOMEWORK_VISION_MODEL,
                                         frames, ledger, checkpoint)

    def summary_figure_client(self):
        if not any(p['name'] == 'deepseek' for p in self.providers):
            return None
        return self._clients['deepseek'], config.HOMEWORK_VISION_MODEL

    def summary_review_client(self):
        """Separate native multimodal review; no text-only provider fallback."""
        if not any(p['name'] == 'deepseek' for p in self.providers):
            return None
        return self._clients['deepseek'], config.HOMEWORK_VISION_MODEL

    def summarize_with_keywords(self, title, content, sources, terms):
        """One summary request also returns separately validated keyword metadata."""
        from src.ai.automatic_glossary import INSTRUCTION, validated_keywords
        errors=[]
        for provider in self.providers:
            client=self._clients[provider['name']]
            for model in provider['models']:
                try:
                    options = ({'extra_body':{'thinking':{'type':'enabled'}},
                                'reasoning_effort':'high'}
                               if provider['name']=='deepseek' else {})
                    response=client.chat.completions.create(model=model,
                        messages=[{'role':'system','content':self.system_prompt+'\n\n'+INSTRUCTION},
                                  {'role':'user','content':json.dumps({'course':title,'material':content,
                                    'evidence_sources':{'asr':'material中的ASR/转写原文',
                                      'ppt':'material中的PPT/OCR原文','cloud':sources.get('cloud',[])},
                                    'historical_terms':terms[:30]},ensure_ascii=False)}],
                        response_format={'type':'json_object'},timeout=600,**options)
                    if not response.choices or response.choices[0].finish_reason!='stop':
                        raise ValueError('Incomplete structured summary')
                    data=json.loads(response.choices[0].message.content)
                    summary=data.get('summary')
                    if not isinstance(summary,str) or not summary.strip():
                        raise ValueError('Empty structured summary')
                    keywords=validated_keywords(data.get('keywords',[]),sources,summary)
                    summary=enrich_summary(summary,api_key=config.TAVILY_API_KEY,client=client,model=model)
                    return summary, f"{provider['name']}/{model}", keywords
                except Exception as error:
                    errors.append(type(error).__name__)
        raise RuntimeError('Structured summary failed: '+','.join(errors))
