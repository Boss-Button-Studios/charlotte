"""Charlotte navigator model adapters.

Shipped adapters:
  GroqAdapter  — Groq API (requires groq extra: pip install charlotte-crawler[groq])
  LocalAdapter — any OpenAI-compatible local endpoint (CHAR-011, not yet implemented)

See spec §6.3 for the adapter contract and BYOM authoring guide.
"""

from charlotte.adapters.base import AdapterProtocol
from charlotte.adapters.groq import GroqAdapter

__all__ = ["AdapterProtocol", "GroqAdapter"]
