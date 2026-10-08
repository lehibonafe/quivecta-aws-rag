
import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config

s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
bucket = os.environ["DOC_BUCKET"]

MAX_BYTES = 2_000_000


def reply(status, data):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(data)
    }


def caller(event):
    # Identity supplied by API Gateway AWS_IAM authorization
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("iam", {})
        .get("userArn")
    )


def handler(event, context):
    owner = caller(event)
    if not owner:
        return reply(401, {"error": "Authentication required"})

    method = event["requestContext"]["http"]["method"]

    if method == "GET":
        document_id = (event.get("pathParameters") or {}).get("id")
        if not document_id:
            return reply(400, {"error": "Missing document ID"})

        result = table.get_item(Key={"document_id": document_id})
        item = result.get("Item")

        if not item or item["owner"] != owner:
            return reply(404, {"error": "Document not found"})

        return reply(200, {
            "document_id": document_id,
            "status": item["status"],
            "filename": item["filename"]
        })

    if method != "POST":
        return reply(405, {"error": "Method not allowed"})

    try:
        data = json.loads(event.get("body") or "{}")
        filename = str(data["filename"]).replace("\\", "/").split("/")[-1]
        size = int(data["size"])
    except (KeyError, ValueError, TypeError):
        return reply(400, {"error": "filename and size are required"})

    extension = os.path.splitext(filename)[1].lower()

    if extension not in {".pdf", ".txt", ".md"}:
        return reply(400, {"error": "Unsupported file type"})

    if size <= 0 or size > MAX_BYTES:
        return reply(400, {"error": "File must be 2 MB or smaller"})

    document_id = str(uuid.uuid4())
    key = f"incoming/{document_id}{extension}"

    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=300,
        HttpMethod="PUT"
    )

    table.put_item(Item={
        "document_id": document_id,
        "owner": owner,
        "filename": filename,
        "s3_key": key,
        "status": "UPLOADING",
        "created_at": datetime.now(timezone.utc).isoformat()
    })

    return reply(200, {
        "document_id": document_id,
        "upload_url": url,
        "status": "UPLOADING"
    })
