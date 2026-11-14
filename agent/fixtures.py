"""Canned post-call payloads for the mock provider. A MINIMAL SUBSET of a real response.

Checked against fixtures/real_post_call.json (a real GET /conversations/{id} response, redacted): the
transcript turn fields, data.status "done", metadata.call_duration_secs / termination_reason, the analysis
keys and the echoed dynamic_variables all exist there. NOT yet verified against a real call: the webhook
envelope (type / event_timestamp / data), because no webhook has been received, and any supplier turn
that contains a confirmation number. "no answer" here is a transcript with no supplier turn; the real
no-answer shape is fixtures/real_no_answer.json (metadata.phone_call.call_result == "no_answer").
"""
import time

SCENARIOS = ("confirmed", "ended_without_number", "supplier_asks_question", "no_answer")

SUPPLIER_REPLY = {
    "confirmed": "Sure, 40 bags, got it. Your confirmation number is MB-48213. Thanks!",
    "ended_without_number": "Okay, got it, thanks. Bye.",
    "supplier_asks_question": "Do you want the 20 kilo bags or the 10 kilo bags?",
    "no_answer": None,
}


def _turn(role: str, message: str, t: int) -> dict:
    return {"role": role, "message": message, "tool_calls": None, "tool_results": None,
            "feedback": None, "time_in_call_secs": t, "conversation_turn_metrics": None}


def post_call_payload(scenario: str, conversation_id: str, brief: dict) -> dict:
    agent_line = (f"Hello, this is an AI assistant calling on behalf of {brief.get('shop_name', 'the shop')}. "
                  f"This call is transcribed. I'd like to order {brief.get('quantity')} {brief.get('unit')}s of "
                  f"{brief.get('item')} at {brief.get('price')} each. Can you confirm the order?")
    transcript = [_turn("agent", agent_line, 0)]
    if SUPPLIER_REPLY[scenario]:
        transcript.append(_turn("user", SUPPLIER_REPLY[scenario], 9))
    now = int(time.time())
    return {
        "type": "post_call_transcription",
        "event_timestamp": now,
        "data": {
            "agent_id": "mock_agent",
            "conversation_id": conversation_id,
            "status": "done",
            "transcript": transcript,
            "metadata": {"start_time_unix_secs": now - 20, "call_duration_secs": 20, "termination_reason": ""},
            "analysis": {"evaluation_criteria_results": {}, "data_collection_results": {},
                         "call_successful": "unknown", "transcript_summary": ""},
            "conversation_initiation_client_data": {"dynamic_variables": dict(brief)},
        },
    }
