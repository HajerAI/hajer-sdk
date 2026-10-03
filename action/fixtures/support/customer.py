"""Tiny customer application: its parser transforms the replayed model answer."""

import httpx


def classify(ticket: str) -> dict[str, str]:
    response = httpx.post("https://api.openai.com/v1/chat/completions", content=ticket)
    return {"priority": str(response.json()["answer"]).upper()}
