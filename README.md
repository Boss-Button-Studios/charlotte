# Charlotte

`charlotte-crawler` is a goal-directed web navigation agent. Given a starting URL and a natural language goal, Charlotte navigates a website purposefully — evaluating each page and deciding which links to follow — until she finds what she is looking for or exhausts her budget.

Charlotte is a library, not a service. She is designed to be imported into any Python project.

---

## Installation

```bash
# Base install (httpx fetcher + BeautifulSoup extractor)
pip install charlotte-crawler

# With Groq cloud adapter (Llama 3 8B — recommended for cloud deployments)
pip install charlotte-crawler[groq]

# With local/Ollama adapter (any OpenAI-compatible endpoint)
pip install charlotte-crawler[ollama]

# With JavaScript rendering
pip install charlotte-crawler[playwright]
playwright install chromium
```

---

## Quick Start

```python
from charlotte import find_link

result = find_link(
    start_url="https://www.example.edu",
    goal="Find the academic calendar page",
    navigation_hint="Usually listed under Parents or Academics in the main navigation",
)

if result.found:
    print(result.urls[0])
```

See `docs/charlotte-spec-v1.2.md` for the full technical specification, all parameters, adapter authoring, and streaming events reference.

---

## Configuration

| Environment Variable        | Default                   | Effect                                              |
|-----------------------------|---------------------------|-----------------------------------------------------|
| `CHARLOTTE_DEFAULT_ADAPTER` | `"groq"`                  | `"groq"` or `"local"` — selects the default adapter |
| `CHARLOTTE_LOCAL_BASE_URL`  | `"http://localhost:11434"` | Base URL for the `LocalAdapter`                    |
| `CHARLOTTE_LOCAL_MODEL`     | `"llama3:8b"`             | Model name for the `LocalAdapter`                  |
| `CHARLOTTE_STREAM`          | `"true"`                  | `"true"` or `"false"` — sets streaming default     |
| `CHARLOTTE_RESPECT_ROBOTS`  | `"true"`                  | `"true"` or `"false"` — sets robots.txt default    |
| `GROQ_API_KEY`              | *(required for GroqAdapter)* | Groq API key                                     |

---

## Licence

MIT — see `LICENSE`.
