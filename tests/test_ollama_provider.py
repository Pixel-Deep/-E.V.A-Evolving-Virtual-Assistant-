from __future__ import annotations

import io
import json
import socket
import urllib.error
import unittest
from unittest import mock

from eva.providers.ollama_provider import (
    OllamaProvider,
    OllamaProviderError,
    _connection_error,
    _http_error,
)


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


class OllamaProviderTests(unittest.TestCase):
    def test_health_check_confirms_api_version_and_exact_model(self):
        provider = OllamaProvider()
        with mock.patch("eva.providers.ollama_provider.urllib.request.urlopen", side_effect=[
            _Response({"version": "0.40.13"}),
            _Response({"models": [{"name": provider.model}]}),
        ]) as opened:
            result = provider.check_health()
        self.assertTrue(result["ok"])
        self.assertTrue(result["reachable"])
        self.assertEqual(result["version"], "0.40.13")
        self.assertTrue(result["model_present"])
        self.assertEqual(opened.call_count, 2)
        self.assertTrue(opened.call_args_list[0].args[0].full_url.endswith("/api/version"))
        self.assertTrue(opened.call_args_list[1].args[0].full_url.endswith("/api/tags"))

    def test_health_check_separates_missing_model_from_unreachable_server(self):
        provider = OllamaProvider()
        with mock.patch("eva.providers.ollama_provider.urllib.request.urlopen", side_effect=[
            _Response({"version": "0.40.13"}), _Response({"models": []}),
        ]):
            result = provider.check_health()
        self.assertFalse(result["ok"])
        self.assertTrue(result["reachable"])
        self.assertEqual(result["error_code"], "OLLAMA_MODEL_NOT_FOUND")

    def test_local_socket_permission_denial_is_not_reported_as_server_stopped(self):
        denied = PermissionError(13, "access denied")
        denied.winerror = 10013
        failure = _connection_error(urllib.error.URLError(denied), "http://localhost:11434")
        self.assertEqual(failure.code, "OLLAMA_CONNECTION_BLOCKED")
        self.assertIn("does not establish that the server is stopped", str(failure))

    def test_connection_refused_and_slow_response_have_distinct_codes(self):
        refused = _connection_error(ConnectionRefusedError("refused"), "http://localhost:11434")
        slow = _connection_error(socket.timeout("late"), "http://localhost:11434")
        self.assertEqual(refused.code, "OLLAMA_UNREACHABLE")
        self.assertEqual(slow.code, "OLLAMA_TIMEOUT")

    def test_http_errors_distinguish_missing_model_bad_request_and_wrong_endpoint(self):
        missing = urllib.error.HTTPError("/api/chat", 404, "not found", {}, io.BytesIO(b'{"error":"model qwen is not found"}'))
        wrong_route = urllib.error.HTTPError("/chat", 404, "not found", {}, io.BytesIO(b'{"error":"page not found"}'))
        bad_request = urllib.error.HTTPError("/api/chat", 400, "bad request", {}, io.BytesIO(b'{"error":"invalid options"}'))
        self.assertEqual(_http_error(missing, "qwen").code, "OLLAMA_MODEL_NOT_FOUND")
        self.assertEqual(_http_error(wrong_route, "qwen").code, "OLLAMA_API_INCOMPATIBLE")
        self.assertEqual(_http_error(bad_request, "qwen").code, "OLLAMA_BAD_REQUEST")

    def test_project_json_uses_native_chat_endpoint_and_configured_model(self):
        provider = OllamaProvider()
        response = _Response({"message": {"content": '{"goal":"test","steps":[]}'}})
        with mock.patch("eva.providers.ollama_provider.urllib.request.urlopen", return_value=response) as opened:
            result = provider._project_json_attempt("sys", "user", timeout=2, num_predict=8,
                                                    temperature=0.1, top_p=0.9)
        request = opened.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, provider.base_url + "/api/chat")
        self.assertEqual(body["model"], provider.model)
        self.assertEqual(result, '{"goal":"test","steps":[]}')

    def test_generation_settings_respect_configured_budgets(self):
        from eva.config import NUM_CTX, PLANNING_NUM_PREDICT, STRUCTURED_NUM_PREDICT
        provider = OllamaProvider()
        response = _Response({"message": {"content": '{"action":"test"}'}})

        # 1. Project JSON respects STRUCTURED_NUM_PREDICT and NUM_CTX
        with mock.patch("eva.providers.ollama_provider.urllib.request.urlopen", return_value=response) as opened:
            provider.generate_project_json("sys", "user", timeout=5)
        body = json.loads(opened.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["options"]["num_predict"], STRUCTURED_NUM_PREDICT)
        self.assertEqual(body["options"]["num_ctx"], NUM_CTX)

        # 2. Agent action uses PLANNING_NUM_PREDICT
        with mock.patch("eva.providers.ollama_provider.urllib.request.urlopen", return_value=response) as opened:
            provider.generate_agent_action("sys", "user", timeout=5)
        body = json.loads(opened.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["options"]["num_predict"], min(PLANNING_NUM_PREDICT, 1024))
        self.assertEqual(body["options"]["num_ctx"], NUM_CTX)


if __name__ == "__main__":
    unittest.main()
