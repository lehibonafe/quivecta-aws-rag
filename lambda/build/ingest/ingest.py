
import io
import json
import os
from urllib.parse import unquote_plus

import boto3
from pypdf import PdfReader

s3 = boto3.client("s3")
bedrock = boto3.client(
    "bedrock-runtime",
    region_name="ap-southeast-2"
)
vectors = boto3.client(
    "s3vectors",
    region_name="ap-southeast-1"
)

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

BUCKET = os.environ["DOC_BUCKET"]
VECTOR_BUCKET = os.environ["VECTOR_BUCKET"]
VECTOR_INDEX = os.environ["VECTOR_INDEX"]
MAX_BYTES = 2_000_000


def extract_text(data, extension):
    if extension == ".pdf":
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    return data.decode("utf-8")


def chunk_text(text, size=1600, overlap=200):
    text = " ".join(text.split())
    step = size - overlap
    return [
        text[start:start + size]
        for start in range(0, len(text), step)
    ]


def create_embedding(text):
    response = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({
            "inputText": text,
            "dimensions": 512,
            "normalize": True
        })
    )

    return json.loads(response["body"].read())["embedding"]


def update_status(document_id, status, count=0):
    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression="SET #s = :status, chunks = :count",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": status,
            ":count": count
        }
    )


def process_document(bucket, key):
    document_id = key.removeprefix("incoming/").split(".")[0]
    item = table.get_item(
        Key={"document_id": document_id}
    ).get("Item")

    if not item or item["s3_key"] != key:
        return

    # S3 notifications are at-least-once.
    if item["status"] == "READY":
        return

    try:
        update_status(document_id, "PROCESSING")

        obj = s3.get_object(Bucket=bucket, Key=key)

        if obj["ContentLength"] > MAX_BYTES:
            raise ValueError("Document exceeds 2 MB")

        content = obj["Body"].read()
        extension = os.path.splitext(key)[1].lower()

        text = extract_text(content, extension)
        chunks = chunk_text(text)

        if not chunks:
            raise ValueError("No readable document text")

        if len(chunks) > 60:
            raise ValueError("Document has too many chunks")

        records = []

        for index, chunk in enumerate(chunks):
            embedding = create_embedding(chunk)

            records.append({
                "key": f"{document_id}#{index}",
                "data": {"float32": embedding},
                "metadata": {
                    "owner": item["owner"],
                    "document_id": document_id,
                    "filename": item["filename"],
                    "chunk": index,
                    "text": chunk
                }
            })

        # Store embeddings in batches
        for offset in range(0, len(records), 25):
            vectors.put_vectors(
                vectorBucketName=VECTOR_BUCKET,
                indexName=VECTOR_INDEX,
                vectors=records[offset:offset + 25]
            )

        update_status(document_id, "READY", len(records))

        print(json.dumps({
            "event": "document_indexed",
            "document_id": document_id,
            "chunks": len(records)
        }))

    except Exception:
        update_status(document_id, "FAILED")
        raise  # SQS will retry the failed message


def handler(event, context):
    for record in event["Records"]:
        message = json.loads(record["body"])

        for entry in message.get("Records", []):
            bucket = entry["s3"]["bucket"]["name"]
            key = unquote_plus(entry["s3"]["object"]["key"])

            if bucket != BUCKET or not key.startswith("incoming/"):
                continue

            process_document(bucket, key)

