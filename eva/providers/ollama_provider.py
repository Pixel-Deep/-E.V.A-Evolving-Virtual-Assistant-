
"""Connect EVA to a local language model through Ollama."""

import json
import urllib.request
import urllib.error
import socket
import threading

_MODEL_REQUEST_LOCK = threading.Lock()

from eva.config import (
    OLLAMA_URL,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT,
    NUM_PREDICT,
    NUM_CTX,
    MODEL_KEEP_ALIVE,
)


class OllamaProvider:
    """Send conversation messages to Ollama."""

    def __init__(self):
        self.url = f"{OLLAMA_URL}/api/chat"
        self.model = OLLAMA_MODEL

    def _build_payload(self, messages, stream):
        """Build the request payload shared by both chat methods."""

        return {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "think": False,
            "keep_alive": MODEL_KEEP_ALIVE,
            "options": {
                "num_predict": NUM_PREDICT,
                "num_ctx": NUM_CTX,
                "repeat_penalty": 1.3,
                "repeat_last_n": 256,
            },
        }

    def chat_stream(self, messages):
        """Yield response text progressively, serializing local model use."""
        acquired = _MODEL_REQUEST_LOCK.acquire(timeout=OLLAMA_TIMEOUT)
        if not acquired:
            raise RuntimeError("EVA's local model is busy; wait for the current task to finish and try again.")
        try:
            yield from self._chat_stream_locked(messages)
        finally:
            _MODEL_REQUEST_LOCK.release()

    def _chat_stream_locked(self, messages):
        """Internal stream implementation; caller must hold the model lock."""
        payload = self._build_payload(messages, stream=True)
        data = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=OLLAMA_TIMEOUT,
            ) as response:

                for raw_line in response:
                    line = raw_line.decode(
                        "utf-8", errors="replace"
                    ).strip()

                    if not line:
                        continue

                    try:
                        result = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if result.get("error"):
                        raise RuntimeError(result["error"])

                    message = result.get("message", {})
                    content = message.get("content", "")

                    if content:
                        yield content

                    if result.get("done", False):
                        break

        except urllib.error.HTTPError as error:
            details = error.read().decode(
                "utf-8", errors="replace"
            )

            if error.code == 404:
                raise RuntimeError(
                    f"Model '{self.model}' may be missing. "
                    "Check with: ollama list"
                ) from error

            raise RuntimeError(
                f"Ollama error ({error.code}): {details}"
            ) from error

        except urllib.error.URLError as error:
            raise RuntimeError(
                "I couldn't connect to Ollama. "
                "Check that Ollama is running."
            ) from error

        except (TimeoutError, socket.timeout) as error:
            raise RuntimeError(
                "EVA timed out waiting for the model. "
                "Try a shorter prompt or check model performance."
            ) from error


    # Error signatures that indicate the model degenerated into a repetition
    # loop rather than a hardware/connectivity problem. These are worth one
    # bounded retry with safer sampling because the same prompt usually
    # succeeds once repetition is suppressed.
    _REPEAT_LOOP_SIGNS = ("token repeat limit", "prediction aborted", "repeat limit reached")

    def _project_json_attempt(self, system_prompt, user_prompt, *, timeout, num_predict, temperature, top_p, seed=None, json_format=True):
        """Single bounded call to Ollama for structured project generation."""
        options = {
            "num_predict": num_predict,
            "num_ctx": max(3072, min(NUM_CTX if NUM_CTX >= 3072 else 4096, 4096)),
            "temperature": temperature,
            "top_p": top_p,
            # Discourage the degenerate token loops that trigger Ollama's
            # "token repeat limit reached" abort in the first place.
            "repeat_penalty": 1.3,
            "repeat_last_n": 256,
        }
        if seed is not None:
            options["seed"] = seed
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "think": False,
            "keep_alive": MODEL_KEEP_ALIVE,
            "options": options,
        }
        if json_format:
            payload["format"] = "json"
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(8_000_000)
            result = json.loads(raw.decode("utf-8", errors="replace"))
            if result.get("error"):
                raise RuntimeError(str(result["error"]))
            message = result.get("message") or {}
            content = message.get("content", "")
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("The local model returned no project content.")
            return content
        except urllib.error.HTTPError as error:
            details = error.read(3000).decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama project generation failed ({error.code}): {details}") from error
        except urllib.error.URLError as error:
            raise RuntimeError("Cannot connect to Ollama. Check that Ollama is running and the model is installed.") from error
        except (TimeoutError, socket.timeout) as error:
            raise RuntimeError(f"EVA stopped waiting after {timeout} seconds to protect responsiveness.") from error

    def generate_project_json(self, system_prompt, user_prompt, *, timeout=150):
        """Generate one structured project response with bounded local inference.

        Project synthesis stays strictly bounded: at most one automatic retry,
        no concurrent model requests, low temperature, and capped context/output.
        The retry only fires for a detected degenerate repetition loop, and uses
        a smaller output budget and a different seed to avoid repeating it.
        """
        timeout = max(20, min(int(timeout), 180))
        # Serialize generation so two callers cannot compete for limited VRAM.
        acquired = _MODEL_REQUEST_LOCK.acquire(timeout=timeout)
        if not acquired:
            raise RuntimeError("EVA's local model is busy; wait for the current task to finish and try again.")
        try:
            try:
                return self._project_json_attempt(
                    system_prompt, user_prompt,
                    timeout=timeout, num_predict=3072, temperature=0.15, top_p=0.85,
                )
            except RuntimeError as first_error:
                if not any(sign in str(first_error).lower() for sign in self._REPEAT_LOOP_SIGNS):
                    raise
                try:
                    return self._project_json_attempt(
                        system_prompt, user_prompt,
                        timeout=timeout, num_predict=2048, temperature=0.35, top_p=0.9, seed=7,
                    )
                except RuntimeError as retry_error:
                    raise RuntimeError(
                        f"{retry_error} (one automatic retry with safer sampling already attempted and also failed)"
                    ) from retry_error
        finally:
            _MODEL_REQUEST_LOCK.release()

    def generate_text(self, system_prompt, user_prompt, *, timeout=150, num_predict=2048):
        """One bounded, non-JSON call used as a last-resort fallback.

        Strict JSON mode is where small local models most often fail (escaping
        long code strings, nested nesting mistakes). Plain text generation with
        a requested fenced code block is far more reliable for ordinary
        single/few-file requests, so this is used only after JSON generation
        has already failed.
        """
        timeout = max(20, min(int(timeout), 180))
        acquired = _MODEL_REQUEST_LOCK.acquire(timeout=timeout)
        if not acquired:
            raise RuntimeError("EVA's local model is busy; wait for the current task to finish and try again.")
        try:
            try:
                return self._project_json_attempt(
                    system_prompt, user_prompt,
                    timeout=timeout, num_predict=num_predict, temperature=0.2, top_p=0.9,
                    json_format=False,
                )
            except RuntimeError as first_error:
                if not any(sign in str(first_error).lower() for sign in self._REPEAT_LOOP_SIGNS):
                    raise
                # A repetition loop inside plain-text output (rare, but the
                # model degenerates under long output budgets) gets one bounded
                # retry with a smaller budget and a different seed, exactly like
                # the JSON path.
                try:
                    return self._project_json_attempt(
                        system_prompt, user_prompt,
                        timeout=timeout, num_predict=min(num_predict, 2048), temperature=0.35, top_p=0.9, seed=11,
                        json_format=False,
                    )
                except RuntimeError as retry_error:
                    raise RuntimeError(
                        f"{retry_error} (one automatic retry with safer sampling already attempted and also failed)"
                    ) from retry_error
        finally:
            _MODEL_REQUEST_LOCK.release()

    def chat(self, messages):
        """Return a complete response using the streaming interface."""

        reply = "".join(self.chat_stream(messages)).strip()

        if not reply:
            return (
                "The model returned an empty response. "
                "Please try again."
            )

        return reply
