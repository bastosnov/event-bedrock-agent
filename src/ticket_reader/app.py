import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
}


dynamodb = boto3.resource("dynamodb")
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    route_key = (event or {}).get("requestContext", {}).get("routeKey")
    raw_path = (event or {}).get("rawPath")
    query_params = (event or {}).get("queryStringParameters") or {}
    headers = (event or {}).get("headers") or {}
    origin = headers.get("origin") or headers.get("Origin")
    logger.info(
        "TicketReader requestId=%s routeKey=%s rawPath=%s origin=%s queryParams=%s",
        request_id,
        route_key,
        raw_path,
        origin or "<none>",
        query_params,
    )
    if origin == "null":
        logger.warning("Request origin is null (file://). Serve frontend over http(s) to avoid CORS issues.")

    if (event or {}).get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        logger.info("OPTIONS preflight handled for requestId=%s", request_id)
        return _response(200, {"ok": True})

    table_name = os.getenv("SUPPORT_TICKETS_TABLE_NAME")
    if not table_name:
        logger.error("Missing SUPPORT_TICKETS_TABLE_NAME for requestId=%s", request_id)
        return _response(500, {"error": "Missing SUPPORT_TICKETS_TABLE_NAME environment variable"})

    try:
        table = dynamodb.Table(table_name)
        ticket_id = (query_params.get("ticketId") or "").strip()
        status = (query_params.get("status") or "").strip()
        logger.info(
            "Resolved filters requestId=%s ticketId=%s status=%s table=%s",
            request_id,
            ticket_id or "<none>",
            status or "<none>",
            table_name,
        )

        if ticket_id:
            item = _get_ticket_by_id(table, ticket_id)
            if not item:
                logger.info("Ticket not found requestId=%s ticketId=%s", request_id, ticket_id)
                return _response(404, {"error": "Ticket not found", "ticketId": ticket_id})
            logger.info("Ticket found requestId=%s ticketId=%s", request_id, ticket_id)
            return _response(200, {"ticket": item})

        if status:
            if status not in ("Open", "Closed"):
                logger.warning("Invalid status filter requestId=%s status=%s", request_id, status)
                return _response(400, {"error": "status must be Open or Closed"})
            tickets = _scan_tickets(table, filter_expression=Attr("status").eq(status))
            logger.info("Status scan complete requestId=%s status=%s count=%d", request_id, status, len(tickets))
            return _response(200, {"tickets": tickets, "count": len(tickets), "status": status})

        tickets = _scan_tickets(table)
        logger.info("Full scan complete requestId=%s count=%d", request_id, len(tickets))
        return _response(200, {"tickets": tickets, "count": len(tickets)})

    except (BotoCoreError, ClientError):
        logger.exception("DynamoDB read failed requestId=%s", request_id)
        return _response(500, {"error": "Failed to read support tickets"})
    except Exception:
        logger.exception("Unhandled TicketReader error requestId=%s", request_id)
        return _response(500, {"error": "Unexpected error while reading support tickets"})


def _get_ticket_by_id(table, ticket_id):
    logger.info("DynamoDB GetItem ticketId=%s", ticket_id)
    result = table.get_item(Key={"ticketId": ticket_id})
    return result.get("Item")


def _scan_tickets(table, filter_expression=None):
    scan_kwargs = {}
    if filter_expression is not None:
        scan_kwargs["FilterExpression"] = filter_expression

    tickets = []
    last_evaluated_key = None

    while True:
        if last_evaluated_key:
            scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

        logger.info("DynamoDB Scan called with filter=%s", bool(filter_expression))
        response = table.scan(**scan_kwargs)
        tickets.extend(response.get("Items", []))

        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break

    return tickets


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(body),
    }
