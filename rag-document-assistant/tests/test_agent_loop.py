import copy
import json
import signal
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.agent_loop import ModelTurn, dispatch_tool, run_investigation, tool_definitions, tool_registry
from app.agent_model import ConfiguredInvestigationModel
from app.case_tools import CaseContext, ToolResult
from app.credit_service import CreditDataError
from app.investigation_tools import HumanReview, InvestigationTools


def turn(name, arguments, call_id="call-1"):
    return ModelTurn({"role": "assistant", "tool_calls": [{
        "id": call_id, "type": "function", "function": {
            "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }]})


class FakeModel:
    def __init__(self, *turns):
        self.turns = iter(turns)
        self.requests = []

    def next_turn(self, messages, tools, *, timeout_seconds):
        self.requests.append(copy.deepcopy(messages))
        return next(self.turns)


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.context = CaseContext(**{key: uuid.uuid4() for key in (
            "tenant_id", "borrower_id", "prior_period_id", "current_period_id",
        )})
        self.session = Mock()
        self.tools = Mock(spec=InvestigationTools)
        self.tools.outcome = None
        self.tools.get_current_fact.return_value = ToolResult(ok=True, data={"value": "330"})
        self.auth = patch("app.agent_loop.authorize_credit_case").start()
        patch("app.agent_loop.evaluate_credit_case", side_effect=CreditDataError()).start()
        patch("app.agent_loop.InvestigationTools", return_value=self.tools).start()
        self.addCleanup(patch.stopall)

    def run_model(self, model, **kwargs):
        return run_investigation(self.session, self.context, model, **kwargs)

    def review(self, arguments):
        outcome = HumanReview(borrower_id=self.context.borrower_id,
            prior_period_id=self.context.prior_period_id, current_period_id=self.context.current_period_id,
            reason=arguments["reason"], unresolved_questions=())
        self.tools.outcome = outcome
        return ToolResult(ok=True, data=outcome)

    def test_review_terminates_and_tool_result_is_returned_before_next_turn(self):
        self.tools.request_human_review.side_effect = self.review
        model = FakeModel(turn("get_current_fact", {"metric": "ebitda"}),
            turn("request_human_review", {"reason": "Missing explanation"}, "call-2"))
        result = self.run_model(model)
        self.assertEqual(result.termination_reason, "human_review")
        self.assertEqual((result.model_turns, result.tool_calls), (2, 2))
        tool_message = model.requests[1][-1]
        self.assertEqual(tool_message["tool_call_id"], "call-1")
        self.assertEqual(json.loads(tool_message["content"])["data"]["value"], "330")

    def test_malformed_json_returns_error_to_model_then_can_recover(self):
        self.tools.request_human_review.side_effect = self.review
        model = FakeModel(turn("get_current_fact", "{broken"),
            turn("request_human_review", {"reason": "No evidence"}, "call-2"))
        result = self.run_model(model)
        self.assertEqual(result.termination_reason, "human_review")
        self.assertEqual(json.loads(model.requests[1][-1]["content"])["error"]["code"], "invalid_arguments")
        self.tools.get_current_fact.assert_not_called()

    def test_unknown_tool_cannot_call_arbitrary_python(self):
        result = self.run_model(FakeModel(turn("__dict__", {})), max_steps=1)
        self.assertEqual(result.termination_reason, "step_limit")
        self.assertEqual(result.steps[-1].result["error"]["code"], "unknown_tool")
        self.assertEqual(self.tools.method_calls, [])

    def test_step_limit_bounds_repeated_requests(self):
        result = self.run_model(FakeModel(*(turn("get_current_fact", {"metric": "ebitda"}, f"c{i}") for i in range(3))), max_steps=2)
        self.assertEqual(result.termination_reason, "step_limit")
        self.assertEqual(result.model_turns, 2)

    def test_tool_limit_bounds_multi_call_response(self):
        message = turn("get_current_fact", {"metric": "ebitda"}).message
        message["tool_calls"].append(turn("get_current_fact", {"metric": "total_debt"}, "call-2").message["tool_calls"][0])
        result = self.run_model(FakeModel(ModelTurn(message)), max_tool_calls=1)
        self.assertEqual(result.termination_reason, "tool_limit")
        self.assertEqual(self.tools.get_current_fact.call_count, 1)

    def test_no_tool_call_is_explicit_failure_not_accepted_finding(self):
        result = self.run_model(FakeModel(ModelTurn({"role": "assistant", "content": "EBITDA fell"})))
        self.assertEqual(result.termination_reason, "no_tool_call")
        self.assertIsNone(result.outcome)

    def test_invalid_envelope_executes_no_calls(self):
        for call in ({"role": "user"}, {"role": "assistant", "tool_calls": "bad"}):
            self.assertEqual(self.run_model(FakeModel(ModelTurn(call))).termination_reason, "invalid_model_response")
        message = turn("get_current_fact", {}).message
        message["tool_calls"] *= 2
        self.assertEqual(self.run_model(FakeModel(ModelTurn(message))).termination_reason, "invalid_model_response")
        self.tools.get_current_fact.assert_not_called()

    def test_duplicate_call_ids_across_turns_are_rejected(self):
        result = self.run_model(FakeModel(turn("get_current_fact", {}), turn("get_current_fact", {})))
        self.assertEqual(result.termination_reason, "invalid_model_response")
        self.assertEqual(result.tool_calls, 1)

    def test_unauthorized_case_prevents_model_call(self):
        self.auth.side_effect = PermissionError()
        model = FakeModel()
        self.assertEqual(self.run_model(model).termination_reason, "scope_unavailable")
        self.assertEqual(model.requests, [])

    def test_provider_error_is_redacted(self):
        model = Mock()
        model.next_turn.side_effect = RuntimeError("secret provider detail")
        result = self.run_model(model)
        self.assertEqual(result.termination_reason, "model_error")
        self.assertNotIn("secret", result.model_dump_json())

    def test_request_timeout_and_total_timeout(self):
        model = Mock()
        model.next_turn.side_effect = TimeoutError()
        self.assertEqual(self.run_model(model).termination_reason, "timeout")
        model.next_turn.side_effect = lambda *a, **kw: time.sleep(1)
        old_handler = signal.getsignal(signal.SIGALRM)
        start = time.monotonic()
        result = self.run_model(model, timeout_seconds=0.03)
        self.assertEqual(result.termination_reason, "timeout")
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(signal.getsignal(signal.SIGALRM), old_handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_total_timeout_also_interrupts_tool_execution(self):
        self.tools.get_current_fact.side_effect = lambda args: time.sleep(1)
        result = self.run_model(FakeModel(turn("get_current_fact", {"metric": "ebitda"})), timeout_seconds=0.03)
        self.assertEqual(result.termination_reason, "timeout")
        self.assertIsNone(result.outcome)

    def test_strict_json_rejects_arrays_duplicates_nonfinite_and_oversize(self):
        registry = tool_registry(self.tools)
        for args in ("[]", '{"metric":"ebitda","metric":"total_debt"}', '{"metric":NaN}', "x" * 16001):
            self.assertEqual(dispatch_tool(registry, "get_current_fact", args)["error"]["code"], "invalid_arguments")
        self.tools.get_current_fact.assert_not_called()

    def test_tool_schemas_are_allowlisted_and_have_no_case_arguments_or_refs(self):
        definitions = tool_definitions(tool_registry(self.tools))
        self.assertEqual(len(definitions), 6)
        for definition in definitions:
            parameters = definition["function"]["parameters"]
            self.assertNotIn("tenant_id", parameters["properties"])
            self.assertNotIn("$ref", json.dumps(parameters))
            self.assertFalse(parameters["additionalProperties"])

    def test_provider_metadata_is_preserved_in_next_request(self):
        first = turn("get_current_fact", {})
        first.message["tool_calls"][0]["extra_content"] = {"google": {"thought_signature": "opaque-signature"}}
        model = FakeModel(first, ModelTurn({"role": "assistant"}))
        self.run_model(model)
        self.assertEqual(model.requests[1][-2], first.message)

    def test_model_adapter_disables_retries_and_preserves_metadata_and_usage(self):
        adapter = ConfiguredInvestigationModel.__new__(ConfiguredInvestigationModel)
        adapter.model_name = "test-chat"
        adapter._client = Mock()
        message = turn("get_current_fact", {}).message
        adapter._client.with_options.return_value.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=Mock(model_dump=Mock(return_value=message)), finish_reason="tool_calls")],
            usage=Mock(model_dump=Mock(return_value={"prompt_tokens": 123})),
        )
        result = adapter.next_turn([], [], timeout_seconds=2)
        adapter._client.with_options.assert_called_once_with(timeout=2, max_retries=0)
        self.assertEqual(result.message, message)
        self.assertEqual(result.usage, {"prompt_tokens": 123})


if __name__ == "__main__":
    unittest.main()
