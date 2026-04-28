import json
import logging
import os
import re
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

bedrock_agent_runtime = boto3.client("bedrock-agent-runtime")

DIRECT_INTENT_RESPONSES = {
    "OUT_OF_SCOPE": "I can only help with ITHS course information and support tickets.",
    "MISSING_CONTEXT": "What problem should I describe in the support ticket?",
    "UNSUPPORTED_ACTION": "I can't do that yet. I can answer course questions or create support tickets.",
    "UNCLEAR": "Can you clarify whether this is about a course or whether you want to create a support ticket?",
}


def lambda_handler(event, context):
    route_key = event.get("requestContext", {}).get("routeKey")
    connection_id = event.get("requestContext", {}).get("connectionId")
    logger.info("Incoming websocket event route=%s connectionId=%s", route_key, connection_id)

    if route_key in ("$connect", "$disconnect"):
        return _response(200, {"message": f"{route_key} handled"})

    if route_key != "sendMessage":
        logger.warning("Unsupported route received: %s", route_key)
        _safe_post_if_possible(
            event,
            {
                "action": "agentResponse",
                "error": f"Unsupported route: {route_key}",
            },
        )
        return _response(400, {"error": f"Unsupported route: {route_key}"})

    body = _parse_json_body(event.get("body"))
    if body is None:
        logger.warning("Invalid JSON body: %s", event.get("body"))
        _safe_post_if_possible(
            event,
            {
                "action": "agentResponse",
                "error": "Invalid JSON body",
            },
        )
        return _response(400, {"error": "Invalid JSON body"})

    message = (body.get("message") or "").strip()
    if not message:
        logger.warning("Missing message in payload: %s", body)
        _safe_post_if_possible(
            event,
            {
                "action": "agentResponse",
                "error": "Missing message",
            },
        )
        return _response(400, {"error": "Missing message"})

    management_endpoint = os.getenv("WEBSOCKET_MANAGEMENT_ENDPOINT")
    agent_id = os.getenv("BEDROCK_AGENT_ID")
    agent_alias_id = os.getenv("BEDROCK_AGENT_ALIAS_ID")
    _log_env_diagnostics(
        management_endpoint=management_endpoint,
        agent_id=agent_id,
        agent_alias_id=agent_alias_id,
    )
    if not management_endpoint:
        logger.error("Missing WEBSOCKET_MANAGEMENT_ENDPOINT environment variable")
        return _response(500, {"error": "Missing required environment variables"})

    session_id = (body.get("sessionId") or "").strip() or str(uuid.uuid4())
    logger.info("Using sessionId=%s for incoming message length=%d", session_id, len(message))

    intent_result = detect_intent(message)
    intent = intent_result["intent"]
    confidence = intent_result["confidence"]
    reason = intent_result["reason"]
    requires_agent = intent_result["requiresAgent"]
    logger.info(
        "Detected intent=%s confidence=%s requiresAgent=%s reason=%s",
        intent,
        confidence,
        requires_agent,
        reason,
    )

    if not requires_agent:
        answer = DIRECT_INTENT_RESPONSES[intent]
        post_result = _safe_post(
            management_endpoint,
            connection_id,
            {
                "action": "agentResponse",
                "sessionId": session_id,
                "intent": intent,
                "confidence": confidence,
                "answer": answer,
                "reason": reason,
            },
        )
        return _finalize_post(post_result, session_id, intent, confidence)

    if not agent_id or not agent_alias_id:
        logger.error(
            "Missing Bedrock env vars. BEDROCK_AGENT_ID set=%s, BEDROCK_AGENT_ALIAS_ID set=%s",
            bool(agent_id),
            bool(agent_alias_id),
        )
        _safe_post(
            management_endpoint,
            connection_id,
            {
                "action": "agentResponse",
                "sessionId": session_id,
                "intent": intent,
                "confidence": confidence,
                "answer": "Server is missing Bedrock agent configuration.",
                "reason": reason,
            },
        )
        return _response(500, {"error": "Missing required environment variables"})

    try:
        answer = _invoke_agent(
            agent_id=agent_id,
            agent_alias_id=agent_alias_id,
            session_id=session_id,
            message=message,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.exception("Bedrock invoke_agent failed")
        _safe_post(
            management_endpoint,
            connection_id,
            {
                "action": "agentResponse",
                "sessionId": session_id,
                "intent": intent,
                "confidence": confidence,
                "answer": "I had trouble reaching the course assistant. Please try again.",
                "reason": reason,
                "error": "Failed to get response from Bedrock Agent",
                "details": str(exc),
            },
        )
        return _response(502, {"error": "Bedrock invocation failed", "sessionId": session_id})

    post_result = _safe_post(
        management_endpoint,
        connection_id,
        {
            "action": "agentResponse",
            "sessionId": session_id,
            "intent": intent,
            "confidence": confidence,
            "answer": answer,
            "reason": reason,
        },
    )

    return _finalize_post(post_result, session_id, intent, confidence)


def _log_env_diagnostics(management_endpoint, agent_id, agent_alias_id):
    logger.info(
        "Env diagnostics region=%s websocketEndpointSet=%s websocketEndpoint=%s bedrockAgentIdSet=%s bedrockAgentId=%s bedrockAliasIdSet=%s bedrockAliasId=%s",
        os.getenv("AWS_REGION") or "<unknown>",
        bool(management_endpoint),
        _mask_value(management_endpoint),
        bool(agent_id),
        _mask_value(agent_id),
        bool(agent_alias_id),
        _mask_value(agent_alias_id),
    )


def _mask_value(value: str) -> str:
    if not value:
        return "<missing>"
    clean = str(value).strip()
    if len(clean) <= 8:
        return clean
    return f"{clean[:4]}...{clean[-4:]}"


def detect_intent(message: str) -> dict:
    text = _normalize_message(message)
    words = [w for w in re.split(r"\W+", text) if w]

    if not text or len(words) <= 1:
        return {
            "intent": "UNCLEAR",
            "confidence": "high",
            "reason": "Message is too short to classify",
            "requiresAgent": False,
        }

    if _contains_any(text, ["weather", "won the game", "game score", "workout plan", "stock price", "bitcoin"]):
        return {
            "intent": "OUT_OF_SCOPE",
            "confidence": "high",
            "reason": "Message is unrelated to ITHS course info or support tickets",
            "requiresAgent": False,
        }

    unsupported_patterns = [
        "register me",
        "enroll me",
        "enrol me",
        "submit my assignment",
        "change my schedule",
        "close my ticket",
        "delete my ticket",
    ]
    if _contains_any(text, unsupported_patterns):
        return {
            "intent": "UNSUPPORTED_ACTION",
            "confidence": "high",
            "reason": "User requested an unsupported action",
            "requiresAgent": False,
        }

    if _contains_any(f" {text} ", [" compare ", " difference ", " different ", " versus ", " vs "]):
        return {
            "intent": "COMPARE_COURSES",
            "confidence": "high",
            "reason": "User is comparing courses",
            "requiresAgent": True,
        }

    if _looks_like_course_list_or_filter(text):
        return {
            "intent": "LIST_OR_FILTER_COURSES",
            "confidence": "high",
            "reason": "User asks for multiple or filtered courses",
            "requiresAgent": True,
        }

    if _is_support_ticket_request_with_context(text):
        return {
            "intent": "CREATE_SUPPORT_TICKET",
            "confidence": "high",
            "reason": "User describes a problem and asks for support ticket/help action",
            "requiresAgent": True,
        }

    if _is_support_ticket_request_missing_context(text):
        return {
            "intent": "MISSING_CONTEXT",
            "confidence": "high",
            "reason": "User asks for support but does not provide a problem description",
            "requiresAgent": False,
        }

    if _looks_like_specific_course_question(text):
        return {
            "intent": "ASK_COURSE_INFO",
            "confidence": "high",
            "reason": "User asks about one specific course",
            "requiresAgent": True,
        }

    if _contains_any(text, ["help", "question", "confusing"]):
        return {
            "intent": "UNCLEAR",
            "confidence": "medium",
            "reason": "Message is too vague to infer if it is course info or ticket creation",
            "requiresAgent": False,
        }

    return {
        "intent": "OUT_OF_SCOPE",
        "confidence": "medium",
        "reason": "Message does not match supported ITHS course or ticket intents",
        "requiresAgent": False,
    }


def _normalize_message(message: str) -> str:
    normalized = message.lower().strip()
    normalized = normalized.replace("’", "'")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _contains_any(text: str, phrases) -> bool:
    return any(phrase in text for phrase in phrases)


def _looks_like_course_list_or_filter(text: str) -> bool:
    list_filter_phrases = [
        "which courses",
        "list courses",
        "show courses",
        "courses in",
        "beginner courses",
        "advanced courses",
        "filter courses",
        "group courses",
        "courses involve",
        "courses use",
    ]
    return _contains_any(text, list_filter_phrases)


def _is_support_ticket_request_with_context(text: str) -> bool:
    support_request_markers = [
        "ticket",
        "support",
        "issue",
        "problem",
        "cannot access",
        "can't access",
        "cant access",
        "blocked",
        "not working",
        "broken",
    ]
    has_support_marker = _contains_any(text, support_request_markers)
    if not has_support_marker:
        return False

    short_context_patterns = {
        "create a ticket",
        "create ticket",
        "open a ticket",
        "open ticket",
        "open a support ticket",
        "create a support ticket",
        "i need help",
    }
    if text in short_context_patterns:
        return False

    return len(text) >= 20


def _is_support_ticket_request_missing_context(text: str) -> bool:
    missing_context_patterns = [
        "create a ticket",
        "create ticket",
        "open a ticket",
        "open ticket",
        "open a support ticket",
        "create a support ticket",
        "i need help",
    ]
    if text in missing_context_patterns:
        return True

    mentions_ticket_or_support = _contains_any(text, ["ticket", "support"])
    short_message = len(text.split()) <= 4
    return mentions_ticket_or_support and short_message


def _looks_like_specific_course_question(text: str) -> bool:
    course_names = [
        "api design",
        "devops",
        "cloud cost management",
        "communication skills",
        "database fundamentals",
        "frontend introduction",
        "project guidelines",
        "security basics",
        "testing principles",
        "version control",
    ]
    asks_course_question = _contains_any(text, ["what is", "what will i learn", "about"])
    mentions_known_course = _contains_any(text, course_names)
    return asks_course_question and (mentions_known_course or "course" in text)


def _finalize_post(post_result, session_id, intent, confidence):
    if post_result == "gone":
        logger.warning("Post result for sessionId=%s: gone", session_id)
        return _response(410, {"error": "WebSocket connection is stale", "sessionId": session_id})
    if post_result == "error":
        logger.error("Post result for sessionId=%s: error", session_id)
        return _response(500, {"error": "Failed to post message to WebSocket", "sessionId": session_id})

    logger.info("Successfully sent response for sessionId=%s", session_id)
    return _response(
        200,
        {
            "ok": True,
            "sessionId": session_id,
            "intent": intent,
            "confidence": confidence,
        },
    )


def _invoke_agent(agent_id, agent_alias_id, session_id, message):
    response = bedrock_agent_runtime.invoke_agent(
        agentId=agent_id,
        agentAliasId=agent_alias_id,
        sessionId=session_id,
        inputText=message,
    )

    chunks = []
    for event in response.get("completion", []):
        chunk = event.get("chunk")
        if not chunk:
            continue
        data = chunk.get("bytes")
        if not data:
            continue
        if isinstance(data, (bytes, bytearray)):
            chunks.append(data.decode("utf-8"))
        else:
            chunks.append(str(data))

    return "".join(chunks).strip() or "I was not able to generate a response."


def _safe_post(endpoint, connection_id, payload):
    api_client = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)

    try:
        api_client.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(payload).encode("utf-8"),
        )
        return "ok"
    except api_client.exceptions.GoneException:
        logger.warning("Connection %s is stale", connection_id)
        return "gone"
    except (BotoCoreError, ClientError):
        logger.exception("Failed to post response to WebSocket connection")
        return "error"


def _safe_post_if_possible(event, payload):
    connection_id = event.get("requestContext", {}).get("connectionId")
    endpoint = os.getenv("WEBSOCKET_MANAGEMENT_ENDPOINT")
    if not connection_id or not endpoint:
        return
    _safe_post(endpoint, connection_id, payload)


def _parse_json_body(body):
    if body is None:
        return {}
    if isinstance(body, dict):
        return body
    if not isinstance(body, str):
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "body": json.dumps(body),
    }
