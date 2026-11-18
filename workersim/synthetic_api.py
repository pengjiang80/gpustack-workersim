"""Bounded, explicitly synthetic embedding, rerank and audio API responses.

These fixtures exercise gateway and playground plumbing, never model quality.
"""

import base64
import hashlib
import io
import math
import struct
import wave

from aiohttp import web

HEADERS = {"X-WorkerSim": "true"}
TEXT = "[WorkerSim] Simulated transcription; no speech recognition was performed."


def bad(message):
    raise web.HTTPBadRequest(text=message)


async def response(request, path, model_name):
    if request.method != "POST":
        return None
    if path == "/v1/embeddings":
        body = await request.json()
        inputs = body.get("input")
        if isinstance(inputs, str) or (
            isinstance(inputs, list) and inputs and all(type(x) is int for x in inputs)
        ):
            inputs = [inputs]
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= 128:
            bad("input must contain 1..128 strings or token sequences")
        if any(
            not isinstance(x, str) and not (isinstance(x, list) and all(type(t) is int for t in x))
            for x in inputs
        ):
            bad("Invalid embedding input")
        dimensions = body.get("dimensions", 1024)
        if type(dimensions) is not int or not 1 <= dimensions <= 4096:
            bad("dimensions must be 1..4096")
        encoding = body.get("encoding_format", "float")
        if encoding not in ("float", "base64"):
            bad("encoding_format must be float or base64")
        data = []
        for index, value in enumerate(inputs):
            seed = hashlib.sha256(str(value).encode()).digest()
            vector = [float(seed[i % len(seed)]) - 127.5 for i in range(dimensions)]
            norm = math.sqrt(sum(v * v for v in vector))
            vector = [v / norm for v in vector]
            if encoding == "base64":
                vector = base64.b64encode(struct.pack(f"<{dimensions}f", *vector)).decode()
            data.append({"object": "embedding", "index": index, "embedding": vector})
        tokens = sum(max(1, len(x) // 4) if isinstance(x, str) else len(x) for x in inputs)
        return web.json_response(
            {
                "object": "list",
                "model": model_name,
                "data": data,
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
                "simulated": True,
            },
            headers=HEADERS,
        )
    if path in ("/v1/rerank", "/rerank"):
        body = await request.json()
        documents = body.get("documents")
        if not isinstance(body.get("query"), str) or not isinstance(documents, list):
            bad("query and documents are required")
        if not 1 <= len(documents) <= 128 or any(
            not isinstance(d, str) and not (isinstance(d, dict) and isinstance(d.get("text"), str))
            for d in documents
        ):
            bad("documents must contain 1..128 strings or text objects")
        top_n = body.get("top_n", len(documents))
        if type(top_n) is not int or not 1 <= top_n <= len(documents):
            bad("top_n must be within the document count")
        # Stable descending fixtures, without implying a semantic similarity calculation.
        rows = []
        for index, document in enumerate(documents[:top_n]):
            row = {"index": index, "relevance_score": 1 / (index + 1)}
            if body.get("return_documents"):
                row["document"] = {"text": document} if isinstance(document, str) else document
            rows.append(row)
        return web.json_response(
            {"model": model_name, "results": rows, "simulated": True}, headers=HEADERS
        )
    if path in ("/v1/audio/transcriptions", "/v1/audio/translations"):
        if request.content_type != "multipart/form-data":
            bad("A multipart audio file is required")
        reader = await request.multipart()
        found_file, response_format = False, "json"
        async for part in reader:
            if part.name == "file":
                found_file = True
                await part.release()
            elif part.name == "response_format":
                response_format = (await part.text())[:32]
            else:
                await part.release()
        if not found_file:
            bad("file is required")
        if response_format in ("text", "srt", "vtt"):
            text = TEXT
            if response_format == "srt":
                text = "1\n00:00:00,000 --> 00:00:00,250\n" + TEXT + "\n"
            elif response_format == "vtt":
                text = "WEBVTT\n\n00:00.000 --> 00:00.250\n" + TEXT + "\n"
            return web.Response(text=text, headers=HEADERS)
        if response_format not in ("json", "verbose_json"):
            bad("Unsupported response_format")
        body = {"text": TEXT, "simulated": True}
        if response_format == "verbose_json":
            body.update(task="transcribe", language="english", duration=0.25, segments=[])
        return web.json_response(body, headers=HEADERS)
    if path == "/v1/audio/speech":
        body = await request.json()
        if not isinstance(body.get("input"), str) or not body["input"]:
            bad("input text is required")
        # Return an identifiable WAV fixture; no encoder or speech synthesis is invoked.
        # Browsers can play this short silence even if the caller's default format is mp3.
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 4000)
        return web.Response(
            body=output.getvalue(),
            content_type="audio/wav",
            headers=HEADERS | {"X-WorkerSim-Audio": "silent-wav-fixture"},
        )
    return None
