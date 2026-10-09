"""Minimal Ollama HTTP client with streaming chat.

Deliberately tiny: this wraps /api/tags (model discovery) and /api/chat
(streaming) and nothing else. No agent frameworks, no tool schemas,
no giant system prompts.
"""

import json

import requests

class OllamaClient:
    def __init__(self, server_url: str = "http://localhost:11434"):
        self.server_url = server_url.rstrip("/")
        self._response = None
        self._aborted = False

    @property
    def aborted(self) -> bool:
        return self._aborted

    def list_models(self) -> list[dict]:
        """Return installed models, smallest first.

        Raises requests.RequestException when the server is unreachable.
        Each item: {"name": str, "size": int}
        """
        resp = requests.get(f"{self.server_url}/api/tags", timeout=3)
        resp.raise_for_status()
        models = [
            {"name": m["name"], "size": m.get("size", 0)}
            for m in resp.json().get("models", [])
        ]
        models.sort(key=lambda m: m["size"])
        return models

    def pick_default_model(self) -> str | None:
        """Prefer the smallest installed coder model, else smallest overall."""
        try:
            models = self.list_models()
        except requests.RequestException:
            return None
        coder = next((m for m in models if "coder" in m["name"].lower()), None)
        if coder is not None:
            return coder["name"]
        return models[0]["name"] if models else None

    def _sanitize_messages(self, messages: list[dict]) -> list[dict]:
        """Strip empty/whitespace-only content to prevent 400 errors."""
        cleaned = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                content = content.strip()
            if content:  # Skip truly empty messages
                cleaned.append({"role": msg["role"], "content": content})
        return cleaned

    def chat(self, model: str, messages: list[dict]):
        """Stream a chat completion, yielding content deltas (str)."""
        self._aborted = False

        # Sanitize to prevent 400 errors from empty content
        messages = self._sanitize_messages(messages)

        if not messages:
            raise ValueError("No valid messages to send to Ollama")

        payload = {"model": model, "messages": messages, "stream": True}
        resp = requests.post(
            f"{self.server_url}/api/chat",
            json=payload,
            stream=True,
            # generous connect timeout, no read timeout: local inference
            # can legitimately pause a long time between tokens
            timeout=(5, None),
        )

        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            # Get detailed error from response body
            try:
                err_body = resp.text
            except Exception:
                err_body = ""
            if err_body:
                raise RuntimeError(f"Ollama API error: {err_body}") from exc
            raise

        self._response = resp
        try:
            for line in resp.iter_lines():
                if self._aborted or not line:
                    continue
                data = json.loads(line)
                if "error" in data:
                    raise RuntimeError(data["error"])
                delta = data.get("message", {}).get("content", "")
                if delta:
                    yield delta
                if data.get("done"):
                    break
        finally:
            self._response = None

    def complete(self, model: str, messages: list) -> str:
        """Non-streaming chat completion; returns the full reply text."""
        self._aborted = False
        messages = self._sanitize_messages(messages)
        if not messages:
            raise ValueError("No valid messages to send to Ollama")
        payload = {"model": model, "messages": messages, "stream": False}
        resp = requests.post(
            f"{self.server_url}/api/chat",
            json=payload,
            timeout=(5, None),
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            try:
                err_body = resp.text
            except Exception:
                err_body = ""
            if err_body:
                raise RuntimeError(f"Ollama API error: {err_body}") from exc
            raise
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        return data.get("message", {}).get("content", "")

    def abort(self) -> None:
        """Cancel an in-flight chat (called from the GUI on Stop)."""
        self._aborted = True
        if self._response is not None:
            self._response.close()
