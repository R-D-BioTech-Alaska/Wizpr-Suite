from __future__ import annotations

import argparse
import asyncio

from ..llm.providers.ollama_provider import OllamaProvider


async def main_async(base_url: str) -> int:
    provider = OllamaProvider(base_url=base_url)
    url, msg = await provider.discover_base_url(base_url)
    if not url:
        print(f"ollama_not_found: {msg}")
        return 2

    print(f"ollama_found: {url} ({msg})")
    models, err = await provider.list_models()
    if err:
        print(f"model_fetch_failed: {err}")
        return 3

    print(f"models: {len(models)}")
    for model in models:
        print(f"  {model}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Find a reachable Ollama server and list installed models.")
    parser.add_argument("--base-url", default="", help="Preferred URL to try first, e.g. http://127.0.0.1:11434.")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args.base_url)))


if __name__ == "__main__":
    main()
