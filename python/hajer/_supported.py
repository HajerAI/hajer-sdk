"""The client constructors `wrap()` captures, by the qualified name an application imports them under.

This is the one list Hajer's install planner wraps at a construction site (the platform pins its own constructor list
equal to this in a test), so the two cannot drift: a type missing here is named in the install PR, never wrapped. Each entry holds its calls where
`wrap()` reads them — `chat.completions` / `responses` / `messages` on the client itself, or the provider client on
`root_client` / `_client` (`_wrap._INNER_CLIENT_ATTRIBUTES`). `tests/test_supported_clients.py` wraps a fake of each.

`wrap()` returns the very object it is handed, instrumented in place, so the planner may wrap a construction
expression wherever the value goes next (a registry, an attribute, another module): `isinstance`, attribute access and
framework composition (LangChain `|`, `.bind_tools`, `.with_structured_output`) are the object's own, and its calls are
still recorded (`tests/test_wrap_compatibility.py`). A type for which that could not hold is not in this list; the
planner then names its construction site `UNSUPPORTED_CLIENT` instead of wrapping it.
"""

from typing import Final

WRAP_CONSTRUCTORS: Final = frozenset(
    {
        "openai.OpenAI",
        "openai.AsyncOpenAI",
        "openai.AzureOpenAI",
        "openai.AsyncAzureOpenAI",
        "anthropic.Anthropic",
        "anthropic.AsyncAnthropic",
        "langchain_openai.ChatOpenAI",
        "langchain_anthropic.ChatAnthropic",
        # Langfuse's OpenAI integration hands out the provider's own classes, patched: the same surface.
        "langfuse.openai.OpenAI",
        "langfuse.openai.AsyncOpenAI",
        "langfuse.openai.AzureOpenAI",
        "langfuse.openai.AsyncAzureOpenAI",
        "langfuse.openai.openai.OpenAI",
        "langfuse.openai.openai.AsyncOpenAI",
        "langfuse.openai.openai.AzureOpenAI",
        "langfuse.openai.openai.AsyncAzureOpenAI",
    }
)
