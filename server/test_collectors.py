"""Inference adapter tests; run with python3 -m unittest discover -s server."""

import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

import collectors
from dev_server import SparkDashboardHandler


LLAMA_PROPS = {
    "model_alias": "qwen3.8-27b",
    "model_path": "/models/qwen-flash.gguf",
    "default_generation_settings": {"n_ctx": 262144},
    "total_slots": 1,
    "endpoint_metrics": True,
}
LLAMA_METRICS = """# HELP llamacpp:predicted_tokens_seconds Average generation throughput
llamacpp:predicted_tokens_seconds 42.5
llamacpp:requests_processing 1
llamacpp:requests_deferred 0
llamacpp:tokens_predicted_total 100
llamacpp:prompt_tokens_total 200
llamacpp:prompt_tokens_cached_total 800
llamacpp:n_tokens_max 99999
llamacpp:spec_decode_num_draft_tokens_total 0
llamacpp:spec_decode_num_accepted_tokens_total 0
llamacpp:spec_decode_num_drafts_total 0
"""
SGLANG_METRICS = """sglang:gen_throughput{model_name="Qwen model"} 51.5
sglang:num_running_reqs{model_name="Qwen model"} 1
sglang:num_queue_reqs{model_name="Qwen model"} 0
sglang:num_used_tokens{model_name="Qwen model"} 1000
sglang:max_total_num_tokens{model_name="Qwen model"} 400000
sglang:spec_accept_rate{model_name="Qwen model"} 0.75
sglang:spec_accept_length{model_name="Qwen model"} 4
sglang:realtime_tokens_total{mode="prefill_cache",model_name="Qwen model"} 600
sglang:realtime_tokens_total{mode="prefill_compute",model_name="Qwen model"} 400
sglang:realtime_tokens_total{mode="decode",model_name="Qwen model"} 50
"""
LLAMA_SLOTS = [{
    "id": 0, "id_task": 1, "n_ctx": 262144, "is_processing": True,
    "speculative": False, "n_prompt_tokens": 1100,
    "next_token": [{"n_decoded": 100}],
}]


class InferenceTests(unittest.TestCase):
    def setUp(self):
        for name in ("INFERENCE_CACHE", "INFERENCE_IDENTITY", "PREFILL_COUNTERS_PREVIOUS", "DECODE_COUNTER_PREVIOUS", "LLAMACPP_SLOTS_PREVIOUS"):
            self.patch_state = patch.object(collectors, name, None)
            self.patch_state.start()
            self.addCleanup(self.patch_state.stop)

    def scrape(self, body=LLAMA_METRICS, props=LLAMA_PROPS, slots=LLAMA_SLOTS, status=200, model_info=None):
        responses = {
            "/metrics": (body, status),
            "/props": (props, 200 if props is not None else 404),
            "/slots": (slots, 200 if slots is not None else 501),
            "/get_model_info": (model_info, 200 if model_info is not None else 404),
        }
        with patch.object(collectors, "read_inference_endpoint", side_effect=lambda path, **_: responses[path]):
            return collectors.read_inference_metrics(force=True)

    def test_detects_llama_and_maps_current_context_without_double_counting(self):
        result = self.scrape()
        self.assertEqual(result["runtime"], "llamacpp")
        self.assertEqual(result["model"], "qwen3.8-27b")
        self.assertIsNone(result["genThroughput"])  # First live sample establishes the baseline.
        self.assertEqual(result["serverGenThroughput"], 42.5)
        self.assertEqual(result["numUsedTokens"], 1100)
        self.assertEqual(result["contextTokens"], 262144)
        self.assertEqual(result["prefixHitRate"], 0.8)
        self.assertIsNone(result["specAcceptRate"])
        self.assertFalse(result["supportsAbort"])

    def test_sglang_labels_with_spaces_and_realtime_counters(self):
        result = self.scrape(SGLANG_METRICS)
        self.assertEqual(result["runtime"], "sglang")
        self.assertEqual(result["model"], "Qwen model")
        self.assertEqual(result["genThroughput"], 51.5)
        self.assertEqual(result["prefixHitRate"], 0.6)
        self.assertEqual(result["specAcceptLength"], 4)
        self.assertTrue(result["supportsAbort"])
        result = self.scrape(SGLANG_METRICS.replace(" 50\n", " 75\n"))
        self.assertEqual(result["decodeDeltaTokens"], 25)

    def test_partial_sglang_scrape_does_not_hide_available_metrics(self):
        result = self.scrape('sglang:num_running_reqs{model_name="Qwen"} 2\n')
        self.assertTrue(result["available"])
        self.assertEqual(result["numRunningReqs"], 2)
        self.assertIsNone(result["genThroughput"])
        self.assertIsNone(result["numQueueReqs"])

    def test_llama_cache_and_generation_deltas(self):
        self.scrape()
        body = LLAMA_METRICS.replace("total 100\n", "total 140\n").replace("total 800\n", "total 900\n").replace("total 200\n", "total 300\n")
        result = self.scrape(body, slots=[{**LLAMA_SLOTS[0], "next_token": [{"n_decoded": 140}]}])
        self.assertEqual(result["decodeDeltaTokens"], 40)
        self.assertEqual(result["prefillCacheDeltaTokens"], 100)
        self.assertEqual(result["prefixHitRate"], 0.5)

    def test_runtime_and_model_switches_reset_counter_baselines(self):
        self.scrape(SGLANG_METRICS)
        self.assertIsNone(self.scrape()["decodeDeltaTokens"])
        new_props = {**LLAMA_PROPS, "model_path": "/models/another.gguf"}
        result = self.scrape(LLAMA_METRICS.replace("total 100\n", "total 300\n"), props=new_props)
        self.assertIsNone(result["decodeDeltaTokens"])
        self.assertEqual(result["prefillCacheDeltaTokens"], 0)

    def test_counter_reset_and_gap_do_not_produce_negative_or_huge_deltas(self):
        self.scrape()
        reset_slots = [{**LLAMA_SLOTS[0], "next_token": [{"n_decoded": 2}]}]
        self.assertIsNone(self.scrape(LLAMA_METRICS.replace("total 100\n", "total 2\n"), slots=reset_slots)["decodeDeltaTokens"])
        self.scrape(body=None, props=None, slots=None, status=None)
        self.assertIsNone(self.scrape()["decodeDeltaTokens"])

    def test_live_slots_advance_while_prometheus_stays_frozen(self):
        # Captured on HAL: the exporter stayed at 1686 tokens / 0 tok/s while
        # task 2629 advanced from 214 to 232 generated tokens in one second.
        body = LLAMA_METRICS.replace("seconds 42.5", "seconds 0").replace("total 100\n", "total 1686\n")
        slot = {
            **LLAMA_SLOTS[0], "id_task": 2629,
            "next_token": [{"n_decoded": 214}],
            "n_prompt_tokens_cache": 40745, "n_prompt_tokens_processed": 1567,
        }
        with patch.object(collectors.time, "monotonic", return_value=100):
            first = self.scrape(body, slots=[slot])
        with patch.object(collectors.time, "monotonic", return_value=101):
            second = self.scrape(body, slots=[{**slot, "next_token": [{"n_decoded": 232}]}])
        self.assertIsNone(first["genThroughput"])
        self.assertEqual(second["genThroughput"], 18)
        self.assertEqual(second["decodeDeltaTokens"], 18)
        self.assertEqual(second["serverGenThroughput"], 0)
        self.assertEqual(second["throughputSource"], "slots")
        self.assertAlmostEqual(first["prefixHitRate"], 40745 / (40745 + 1567))
        self.assertEqual(second["prefixHitRateSource"], "slots")

    def test_live_speed_sums_slots_and_supports_object_next_token(self):
        slots = [LLAMA_SLOTS[0], {**LLAMA_SLOTS[0], "id": 1, "next_token": {"n_decoded": 50}}]
        with patch.object(collectors.time, "monotonic", return_value=10):
            collectors.llamacpp_slot_activity(slots)
        advanced = [
            {**slots[0], "next_token": [{"n_decoded": 130}]},
            {**slots[1], "next_token": {"n_decoded": 70}},
        ]
        with patch.object(collectors.time, "monotonic", return_value=12):
            result = collectors.llamacpp_slot_activity(advanced)
        self.assertEqual(result, {"genThroughput": 25, "decodeDeltaTokens": 50})

    def test_slot_reuse_does_not_subtract_previous_request_tokens(self):
        with patch.object(collectors.time, "monotonic", return_value=10):
            collectors.llamacpp_slot_activity(LLAMA_SLOTS)
        reused = [{**LLAMA_SLOTS[0], "id_task": 2, "next_token": [{"n_decoded": 8}]}]
        with patch.object(collectors.time, "monotonic", return_value=11):
            result = collectors.llamacpp_slot_activity(reused)
        self.assertEqual(result["genThroughput"], 8)

    def test_completed_request_does_not_count_committed_metrics_again(self):
        with patch.object(collectors.time, "monotonic", return_value=10):
            self.scrape()
        completed = [{**LLAMA_SLOTS[0], "is_processing": False, "next_token": [{"n_decoded": 0}]}]
        with patch.object(collectors.time, "monotonic", return_value=11):
            result = self.scrape(LLAMA_METRICS.replace("total 100\n", "total 1200\n"), slots=completed)
        self.assertEqual(result["genThroughput"], 0)
        self.assertEqual(result["decodeDeltaTokens"], 0)

    def test_long_poll_gap_or_missing_slots_requires_a_new_baseline(self):
        with patch.object(collectors.time, "monotonic", return_value=10):
            collectors.llamacpp_slot_activity(LLAMA_SLOTS)
        with patch.object(collectors.time, "monotonic", return_value=100):
            result = collectors.llamacpp_slot_activity(LLAMA_SLOTS)
        self.assertIsNone(result["genThroughput"])
        self.assertIsNone(collectors.llamacpp_slot_activity(None))
        self.assertIsNone(collectors.llamacpp_slot_activity(LLAMA_SLOTS)["genThroughput"])

    def test_slots_disabled_retains_explicit_server_average_fallback(self):
        result = self.scrape(slots=None)
        self.assertEqual(result["genThroughput"], 42.5)
        self.assertEqual(result["throughputSource"], "metrics")
        self.assertIn("Fallback", result["metricNotes"]["throughput"])

    def test_older_llama_without_cache_or_slots_preserves_unknown_values(self):
        body = "\n".join(line for line in LLAMA_METRICS.splitlines() if "cached_total" not in line)
        result = self.scrape(body, props=None, slots=None)
        self.assertTrue(result["available"])
        for key in ("prefixHitRate", "prefillCacheDeltaTokens", "numUsedTokens", "contextTokens"):
            self.assertIsNone(result[key], key)
        self.assertIn("historical maximum", result["metricNotes"]["context"])

    def test_speculative_acceptance_includes_target_token_in_mean_length(self):
        body = LLAMA_METRICS.replace("draft_tokens_total 0", "draft_tokens_total 80").replace("accepted_tokens_total 0", "accepted_tokens_total 60").replace("drafts_total 0", "drafts_total 20")
        result = self.scrape(body, slots=[{**LLAMA_SLOTS[0], "speculative": True}])
        self.assertEqual(result["specAcceptRate"], 0.75)
        self.assertEqual(result["specAcceptLength"], 4)

    def test_multiple_slots_use_active_occupancy_and_total_capacity(self):
        slots = [
            {**LLAMA_SLOTS[0], "n_ctx": 4096, "n_prompt_tokens": 1000},
            {**LLAMA_SLOTS[0], "id": 1, "n_ctx": 4096, "n_prompt_tokens": 2000},
            {**LLAMA_SLOTS[0], "id": 2, "n_ctx": 4096, "n_prompt_tokens": 3000, "is_processing": False},
        ]
        result = self.scrape(LLAMA_METRICS.replace("requests_processing 1", "requests_processing 2"), slots=slots)
        self.assertEqual(result["numUsedTokens"], 3000)
        self.assertEqual(result["maxTotalNumTokens"], 12288)

    def test_disabled_metrics_can_still_identify_llama_and_read_slots(self):
        result = self.scrape(None, props={**LLAMA_PROPS, "endpoint_metrics": False}, status=501)
        self.assertEqual(result["runtime"], "llamacpp")
        self.assertTrue(result["available"])
        self.assertEqual(result["numRunningReqs"], 1)
        self.assertIsNone(result["genThroughput"])
        self.assertIn("--metrics", result["message"])

    def test_disabled_sglang_metrics_identified_by_native_model_endpoint(self):
        result = self.scrape(None, props=None, slots=None, status=404, model_info={"model_path": "Qwen", "is_generation": True})
        self.assertEqual(result["runtime"], "sglang")
        self.assertFalse(result["available"])
        self.assertIn("--enable-metrics", result["message"])

    def test_unknown_and_unavailable_servers_are_not_assumed_to_be_sglang(self):
        for body, status, message in ((None, None, "unreachable"), (None, 503, "loading"), (None, 401, "authentication"), ("python_info 1", 200, "Unrecognized")):
            with self.subTest(status=status):
                result = self.scrape(body, props=None, slots=None, status=status)
                self.assertIsNone(result["runtime"])
                self.assertFalse(result["available"])
                self.assertIsNone(result["contextTokens"])
                self.assertIn(message, result["message"])

    def test_nonfinite_and_invalid_metrics_remain_null_in_valid_json(self):
        for value in ("NaN", "+Inf", "-Inf", "garbage", "-1"):
            result = self.scrape("llamacpp:predicted_tokens_seconds " + value, props=None, slots=None)
            self.assertIsNone(result["genThroughput"])
            json.dumps(result, allow_nan=False)

    def test_prometheus_timestamp_tab_and_escaped_label(self):
        body = 'sglang:gen_throughput{model_name="Qwen \\"test\\""}\t4.2e1 123456\n'
        self.assertEqual(collectors.parse_prometheus_metrics(body, {"sglang:gen_throughput": "speed"})["speed"], 42)
        self.assertEqual(collectors.parse_prometheus_label(body, "model_name"), 'Qwen "test"')

    def test_near_simultaneous_reads_share_one_sample(self):
        with patch.object(collectors, "read_inference_endpoint", return_value=(SGLANG_METRICS, 200)) as fetch:
            first = collectors.read_inference_metrics()
            second = collectors.read_inference_metrics()
        self.assertIs(first, second)
        self.assertEqual(fetch.call_count, 1)

    def test_http_json_errors_are_isolated(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.read.return_value = b"not JSON"
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(collectors.read_inference_endpoint("/props", parse_json=True), (None, 200))
        error = urllib.error.HTTPError("http://localhost/metrics", 501, "disabled", {}, io.BytesIO())
        with patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(collectors.read_inference_endpoint("/metrics"), (None, 501))


class AbortTests(unittest.TestCase):
    def test_llama_cannot_trigger_sglang_pause_or_continue(self):
        handler = Mock(force_mock=False)
        with patch("dev_server.read_inference_metrics", return_value={"runtime": "llamacpp", "supportsAbort": False, "source": "llamacpp_metrics"}):
            SparkDashboardHandler.send_inference_abort(handler)
        handler.post_sglang_control.assert_not_called()
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 409)

    def test_sglang_abort_and_resume_are_preserved(self):
        handler = Mock(force_mock=False)
        handler.post_sglang_control.return_value = None
        with patch("dev_server.read_inference_metrics", return_value={"runtime": "sglang", "supportsAbort": True}):
            SparkDashboardHandler.send_inference_abort(handler)
        self.assertEqual(handler.post_sglang_control.call_count, 2)
        self.assertEqual(handler.post_sglang_control.call_args_list[0].args[1], {"mode": "abort"})
        self.assertTrue(handler.send_json.call_args.args[0]["ok"])


if __name__ == "__main__":
    unittest.main()
