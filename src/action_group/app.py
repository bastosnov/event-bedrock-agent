import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

VALID_STATUSES = {"Open", "Closed"}

dynamodb = boto3.resource("dynamodb")


def lambda_handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    logger.info(
        "Incoming Bedrock action request: %s",
        json.dumps(
            {
                "actionGroup": event.get("actionGroup"),
                "apiPath": event.get("apiPath"),
                "httpMethod": event.get("httpMethod"),
                "sessionId": event.get("sessionId"),
                "messageVersion": event.get("messageVersion"),
                "parametersType": type(event.get("parameters")).__name__,
                "hasRequestBody": bool(event.get("requestBody")),
            },
            ensure_ascii=False,
        ),
    )
    logger.debug("Incoming raw event: %s", _truncate(json.dumps(event, default=str, ensure_ascii=False), 6000))

    table_name = os.getenv("SUPPORT_TICKETS_TABLE_NAME")
    if not table_name:
        return _build_response(event, 500, {"error": "Missing SUPPORT_TICKETS_TABLE_NAME environment variable"})

    parameters = _parse_parameters(event)
    logger.info("Parsed parameters: %s", json.dumps(parameters, ensure_ascii=False))
    description = (parameters.get("description") or "").strip()
    status = (parameters.get("status") or "Open").strip() or "Open"

    if not description:
        return _build_response(event, 400, {"error": "Missing required field: description"})

    if status not in VALID_STATUSES:
        return _build_response(event, 400, {"error": "status must be Open or Closed"})

    ticket = {
        "ticketId": str(uuid.uuid4()),
        "description": description,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }

    try:
        table = dynamodb.Table(table_name)
        table.put_item(Item=ticket)
    except (BotoCoreError, ClientError):
        logger.exception("Failed to create support ticket")
        return _build_response(event, 500, {"error": "Failed to create ticket"})

    logger.info("Ticket created: ticketId=%s status=%s", ticket["ticketId"], ticket["status"])
    return _build_response(
        event,
        200,
        {
            "message": "Support ticket created successfully",
            "ticket": ticket,
        },
    )


def _parse_parameters(event: Dict[str, Any]) -> Dict[str, Any]:
    def _set_if_value(target: Dict[str, Any], key: str, value: Any) -> None:
        if value is not None and str(value).strip() != "":
            target[key] = str(value)

    result: Dict[str, Any] = {}
    parameters = event.get("parameters") or {}

    if isinstance(parameters, list):
        for item in parameters:
            name = (item or {}).get("name")
            value = (item or {}).get("value")
            if not name:
                continue
            key = str(name).strip().lower()
            if key in {"description", "problemdescription", "problem_description"}:
                _set_if_value(result, "description", value)
            elif key == "status":
                _set_if_value(result, "status", value)

    elif isinstance(parameters, dict):
        _set_if_value(
            result,
            "description",
            parameters.get("description")
            or parameters.get("problemDescription")
            or parameters.get("problem_description"),
        )
        _set_if_value(result, "status", parameters.get("status"))

    body = event.get("body") or event.get("requestBody")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = None

    if isinstance(body, dict):
        _set_if_value(
            result,
            "description",
            body.get("description")
            or body.get("problemDescription")
            or body.get("problem_description"),
        )
        _set_if_value(result, "status", body.get("status"))

    content = (event.get("requestBody") or {}).get("content", {})
    app_json = content.get("application/json") or content.get("application/json; charset=utf-8")

    if isinstance(app_json, dict):
        props = app_json.get("properties")

        if isinstance(props, list):
            for item in props:
                name = (item or {}).get("name")
                value = (item or {}).get("value")
                if not name:
                    continue
                key = str(name).strip().lower()
                if key in {"description", "problemdescription", "problem_description"}:
                    _set_if_value(result, "description", value)
                elif key == "status":
                    _set_if_value(result, "status", value)

        elif isinstance(props, dict):
            for name, item in props.items():
                key = str(name).strip().lower()
                value = (item or {}).get("value") if isinstance(item, dict) else item
                if key in {"description", "problemdescription", "problem_description"}:
                    _set_if_value(result, "description", value)
                elif key == "status":
                    _set_if_value(result, "status", value)

    return result


def _build_response(event: Dict[str, Any], status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(
        "Returning action response: %s",
        json.dumps(
            {
                "actionGroup": event.get("actionGroup"),
                "apiPath": event.get("apiPath"),
                "httpMethod": event.get("httpMethod"),
                "httpStatusCode": status_code,
                "body": body,
            },
            ensure_ascii=False,
        ),
    )
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup"),
            "apiPath": event.get("apiPath"),
            "httpMethod": event.get("httpMethod"),
            "httpStatusCode": status_code,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(body, ensure_ascii=False)
                }
            },
        },
        "sessionAttributes": event.get("sessionAttributes", {}),
        "promptSessionAttributes": event.get("promptSessionAttributes", {}),
    }


def _truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return f"{value[:max_len]}...<truncated>"
