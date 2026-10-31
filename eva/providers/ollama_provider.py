
"""Connect EVA to a local language model through Ollama."""

import json
import urllib.request
import urllib.error
import socket
import threading
import errno
from time import monotonic
from urllib.parse import urlsplit

_MODEL_REQUEST_LOCK = threading.Lock()


class OllamaProviderError(RuntimeError):
    """A classified Ollama connection, API, or generation failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _http_error(error: urllib.error.HTTPError, model: str) -> OllamaProviderError:
    body = error.read(3000).decode("utf-8", errors="replace")
    try:
        parsed = json.loads(body)
        detail = str(parsed.get("error") or body).strip()
    except (ValueError, AttributeError):
        detail = body.strip()
    lowered = detail.casefold()
    if error.code == 404 and ("model" in lowered and any(term in lowered for term in ("not found", "pull"))):
        return OllamaProviderError("OLLAMA_MODEL_NOT_FOUND", f"Ollama is reachable, but model '{model}' is unavailable. {detail}")
    if error.code == 404:
        return OllamaProviderError("OLLAMA_API_INCOMPATIBLE", f"Ollama returned 404 for the configured API endpoint. Check the base URL and native /api/chat compatibility. {detail}")
    if error.code == 400:
        return OllamaProviderError("OLLAMA_BAD_REQUEST", f"Ollama rejected the request (HTTP 400). Check the model/API options and prompt size. {detail}")
    if error.code >= 500:
        return OllamaProviderError("OLLAMA_GENERATION_FAILED", f"Ollama returned HTTP {error.code} while processing the request. {detail}")
    return OllamaProviderError("OLLAMA_HTTP_ERROR", f"Ollama returned HTTP {error.code}. {detail}")


def _connection_error(error: BaseException, endpoint: str) -> OllamaProviderError:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return OllamaProviderError("OLLAMA_TIMEOUT", f"Timed out waiting for Ollama at {endpoint}; the server may be reachable but slow.")
    winerror = getattr(reason, "winerror", None)
    err_no = getattr(reason, "errno", None)
    if winerror == 10013 or err_no in {errno.EACCES, errno.EPERM}:
        return OllamaProviderError("OLLAMA_CONNECTION_BLOCKED", f"The operating system or execution sandbox denied access to local Ollama at {endpoint}. This does not establish that the server is stopped.")
    if isinstance(reason, ConnectionRefusedError) or winerror == 10061 or err_no == errno.ECONNREFUSED:
        return OllamaProviderError("OLLAMA_UNREACHABLE", f"No Ollama service accepted a connection at {endpoint}. Check the configured address and whether Ollama is running.")
    if isinstance(reason, socket.gaierror):
        return OllamaProviderError("OLLAMA_HOST_RESOLUTION_FAILED", f"Could not resolve the Ollama host in {endpoint}: {reason}")
    return OllamaProviderError("OLLAMA_CONNECTION_FAILED", f"Could not connect to Ollama at {endpoint}: {type(reason).__name__}: {reason}")

from eva.config import (
    OLLAMA_URL,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT,
    NUM_PREDICT,
    NUM_CTX,
    PLANNING_NUM_PREDICT,
    STRUCTURED_NUM_PREDICT,
    MODEL_KEEP_ALIVE,
)


class OllamaProvider:
    """Send conversation messages to the configured local Ollama service.

    `supports_agent_loop` advertises the strict one-tool-per-turn JSON interface
    used by EVA's automation runtime. No remote model provider is involved.
    """

    supports_agent_loop = True

    def __init__(self):
        parts = urlsplit(OLLAMA_URL)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("EVA_OLLAMA_URL must be an HTTP(S) base URL without credentials, query, or fragment.")
        self.base_url = OLLAMA_URL
        self.url = f"{self.base_url}/api/chat"
        self.model = OLLAMA_MODEL

    def check_health(self, timeout: float = 3.0) -> dict:
        """Check the native Ollama API and configured model without generating text."""
        timeout = max(0.2, min(float(timeout), 10.0))
        try:
            version_req = urllib.request.Request(f"{self.base_url}/api/version", headers={"Accept": "application/json"})
            with urllib.request.urlopen(version_req, timeout=timeout) as response:
                version = json.loads(response.read(100_000).decode("utf-8", errors="replace"))
            tags_req = urllib.request.Request(f"{self.base_url}/api/tags", headers={"Accept": "application/json"})
            with urllib.request.urlopen(tags_req, timeout=timeout) as response:
                tags = json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
            models = [str(item.get("name", "")) for item in tags.get("models", []) if isinstance(item, dict) and item.get("name")]
            present = self.model in models
            return {"ok": present, "reachable": True, "version": version.get("version"), "endpoint": self.base_url,
                    "model": self.model, "model_present": present, "available_models": models,
                    "error_code": None if present else "OLLAMA_MODEL_NOT_FOUND",
                    "message": "Ollama and the configured model are available." if present else f"Ollama is reachable, but model '{self.model}' is not installed. Available models: {', '.join(models) or '(none)'}"}
        except urllib.error.HTTPError as error:
            failure = _http_error(error, self.model)
            return {"ok": False, "reachable": True, "endpoint": self.base_url, "model": self.model,
                    "model_present": False, "error_code": failure.code, "message": str(failure)}
        except (urllib.error.URLError, OSError, TimeoutError, socket.timeout, ValueError) as error:
            failure = _connection_error(error, self.base_url) if isinstance(error, (urllib.error.URLError, OSError, TimeoutError, socket.timeout)) else OllamaProviderError("OLLAMA_INVALID_RESPONSE", f"Ollama returned invalid health-check JSON: {error}")
            return {"ok": False, "reachable": failure.code == "OLLAMA_INVALID_RESPONSE", "endpoint": self.base_url, "model": self.model,
                    "model_present": False, "error_code": failure.code, "message": str(failure)}

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
        start_time = monotonic()
        acquired = _MODEL_REQUEST_LOCK.acquire(timeout=OLLAMA_TIMEOUT)
        if not acquired:
            raise RuntimeError("EVA's local model is busy; wait for the current task to finish and try again.")
        try:
            remaining_timeout = max(1.0, OLLAMA_TIMEOUT - (monotonic() - start_time))
            yield from self._chat_stream_locked(messages, timeout=remaining_timeout)
        finally:
            _MODEL_REQUEST_LOCK.release()

    def _chat_stream_locked(self, messages, timeout=OLLAMA_TIMEOUT):
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
                timeout=timeout,
            ) as response:
                deadline = monotonic() + timeout
                raw_stream = getattr(response, "fp", None)
                raw_socket = getattr(getattr(raw_stream, "raw", None), "_sock", None)
                saw_valid_payload = False
                malformed_lines = 0
                while True:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise socket.timeout("Ollama stream exceeded its overall request deadline")
                    if raw_socket is not None:
                        raw_socket.settimeout(remaining)
                    raw_line = response.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode(
                        "utf-8", errors="replace"
                    ).strip()

                    if not line:
                        continue

                    try:
                        result = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_lines += 1
                        continue

                    if not isinstance(result, dict):
                        malformed_lines += 1
                        continue
                    saw_valid_payload = True

                    if result.get("error"):
                        message = str(result["error"])
                        if "model" in message.casefold() and any(term in message.casefold() for term in ("not found", "pull")):
                            raise OllamaProviderError("OLLAMA_MODEL_NOT_FOUND", message)
                        raise OllamaProviderError("OLLAMA_GENERATION_FAILED", message)

                    message = result.get("message", {})
                    if not isinstance(message, dict):
                        raise OllamaProviderError("OLLAMA_INVALID_RESPONSE", "Ollama returned a chat event with an invalid message object.")
                    content = message.get("content", "")
                    if not isinstance(content, str):
                        raise OllamaProviderError("OLLAMA_INVALID_RESPONSE", "Ollama returned non-text chat content.")

                    if content:
                        yield content

                    if result.get("done", False):
                        break
                if malformed_lines and not saw_valid_payload:
                    raise OllamaProviderError("OLLAMA_INVALID_RESPONSE", "Ollama returned malformed streaming JSON events.")

        except urllib.error.HTTPError as error:
            raise _http_error(error, self.model) from error

        except (urllib.error.URLError, OSError, TimeoutError, socket.timeout) as error:
            raise _connection_error(error, self.url) from error


    # Error signatures that indicate the model degenerated into a repetition
    # loop rather than a hardware/connectivity problem. These are worth one
    # bounded retry with safer sampling because the same prompt usually
    # succeeds once repetition is suppressed.
    _REPEAT_LOOP_SIGNS = ("token repeat limit", "prediction aborted", "repeat limit reached")

    def _project_json_attempt(self, system_prompt, user_prompt, *, timeout, num_predict, temperature, top_p, seed=None, json_format=True):
        """Single bounded call to Ollama for structured project generation."""
        options = {
            "num_predict": num_predict,
            "num_ctx": NUM_CTX,
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
            raise _http_error(error, self.model) from error
        except (urllib.error.URLError, OSError, TimeoutError, socket.timeout) as error:
            raise _connection_error(error, self.url) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise OllamaProviderError("OLLAMA_INVALID_RESPONSE", "Ollama returned a malformed JSON response.") from error

    def generate_project_json(self, system_prompt, user_prompt, *, timeout=150):
        """Generate one structured project response with bounded local inference.

        Project synthesis stays strictly bounded: at most one automatic retry,
        no concurrent model requests, low temperature, and configurable output budgets.
        The retry only fires for a detected degenerate repetition loop.
        """
        total_timeout = max(10, min(int(timeout), 180))
        deadline = monotonic() + total_timeout
        remaining = max(0.1, deadline - monotonic())
        acquired = _MODEL_REQUEST_LOCK.acquire(timeout=remaining)
        if not acquired:
            raise RuntimeError("EVA's local model is busy; wait for the current task to finish and try again.")
        try:
            http_timeout = max(1.0, deadline - monotonic())
            primary_predict = STRUCTURED_NUM_PREDICT
            try:
                return self._project_json_attempt(
                    system_prompt, user_prompt,
                    timeout=http_timeout, num_predict=primary_predict, temperature=0.15, top_p=0.85,
                )
            except RuntimeError as first_error:
                if not any(sign in str(first_error).lower() for sign in self._REPEAT_LOOP_SIGNS):
                    raise
                retry_timeout = max(1.0, deadline - monotonic())
                if retry_timeout < 2.0:
                    raise first_error
                try:
                    retry_predict = max(256, STRUCTURED_NUM_PREDICT // 2)
                    return self._project_json_attempt(
                        system_prompt, user_prompt,
                        timeout=retry_timeout, num_predict=retry_predict, temperature=0.35, top_p=0.9, seed=7,
                    )
                except RuntimeError as retry_error:
                    raise RuntimeError(
                        f"{retry_error} (one automatic retry with safer sampling already attempted and also failed)"
                    ) from retry_error
        finally:
            _MODEL_REQUEST_LOCK.release()

    def generate_agent_action(self, system_prompt, user_prompt, *, timeout=45):
        """Return one strict JSON agent decision from the local model."""
        total_timeout = max(5, min(int(timeout), 60))
        deadline = monotonic() + total_timeout
        remaining = max(0.1, deadline - monotonic())
        acquired = _MODEL_REQUEST_LOCK.acquire(timeout=remaining)
        if not acquired:
            raise RuntimeError("EVA's local model is busy; wait for the current task to finish and try again.")
        try:
            http_timeout = max(1.0, deadline - monotonic())
            predict_budget = min(PLANNING_NUM_PREDICT, 1024)
            return self._project_json_attempt(
                system_prompt, user_prompt,
                timeout=http_timeout, num_predict=predict_budget, temperature=0.1, top_p=0.85,
            )
        finally:
            _MODEL_REQUEST_LOCK.release()

    def generate_text(self, system_prompt, user_prompt, *, timeout=150, num_predict=None):
        """One bounded, non-JSON call used as a last-resort fallback."""
        if num_predict is None:
            num_predict = STRUCTURED_NUM_PREDICT
        total_timeout = max(10, min(int(timeout), 180))
        deadline = monotonic() + total_timeout
        remaining = max(0.1, deadline - monotonic())
        acquired = _MODEL_REQUEST_LOCK.acquire(timeout=remaining)
        if not acquired:
            raise RuntimeError("EVA's local model is busy; wait for the current task to finish and try again.")
        try:
            http_timeout = max(1.0, deadline - monotonic())
            try:
                return self._project_json_attempt(
                    system_prompt, user_prompt,
                    timeout=http_timeout, num_predict=num_predict, temperature=0.2, top_p=0.9,
                    json_format=False,
                )
            except RuntimeError as first_error:
                if not any(sign in str(first_error).lower() for sign in self._REPEAT_LOOP_SIGNS):
                    raise
                retry_timeout = max(1.0, deadline - monotonic())
                if retry_timeout < 2.0:
                    raise first_error
                try:
                    return self._project_json_attempt(
                        system_prompt, user_prompt,
                        timeout=retry_timeout, num_predict=max(256, num_predict // 2), temperature=0.35, top_p=0.9, seed=11,
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
