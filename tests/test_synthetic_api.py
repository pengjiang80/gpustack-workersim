import base64
import io
import math
import wave
from types import SimpleNamespace

import aiohttp
import pytest
import pytest_asyncio
from conftest import free_port

from workersim.http_server import engine_app, listen


@pytest_asyncio.fixture
async def engine():
    record = SimpleNamespace(
        phase="running",
        online=lambda: True,
        instance={"model_name": "workersim-fixture"},
        traffic=None,
    )
    port = free_port()
    runner = await listen(engine_app(record), "127.0.0.1", port)
    async with aiohttp.ClientSession() as client:
        yield client, f"http://127.0.0.1:{port}", record
    await runner.cleanup()


@pytest.mark.asyncio
async def test_embeddings_batch_encoding_and_fencing(engine):
    client, url, record = engine
    body = {"input": ["first", "second"], "dimensions": 32}
    async with client.post(url + "/v1/embeddings", json=body) as response:
        assert response.status == 200 and response.headers["X-WorkerSim"] == "true"
        data = await response.json()
        assert data["simulated"] and len(data["data"]) == 2
        vectors = [row["embedding"] for row in data["data"]]
        assert vectors[0] != vectors[1]
        assert math.isclose(sum(x * x for x in vectors[0]), 1)
    body.update(encoding_format="base64")
    async with client.post(url + "/v1/embeddings", json=body) as response:
        data = await response.json()
        assert len(base64.b64decode(data["data"][0]["embedding"])) == 32 * 4
    record.phase = "stopped"
    async with client.post(url + "/v1/embeddings", json=body) as response:
        assert response.status == 503


@pytest.mark.asyncio
async def test_rerank_synthetic_order_and_document_return(engine):
    client, url, _ = engine
    async with client.post(
        url + "/v1/rerank",
        json={
            "query": "example",
            "documents": ["one", {"text": "two"}],
            "top_n": 2,
            "return_documents": True,
        },
    ) as response:
        body = await response.json()
        assert response.status == 200 and body["simulated"]
        assert [r["index"] for r in body["results"]] == [0, 1]
        assert body["results"][1]["document"] == {"text": "two"}


@pytest.mark.asyncio
async def test_audio_transcription_and_playable_silent_wav(engine):
    client, url, _ = engine
    async with client.post(url + "/v1/audio/speech", json={"input": "example"}) as response:
        assert response.status == 200 and response.content_type == "audio/wav"
        assert response.headers["X-WorkerSim-Audio"] == "silent-wav-fixture"
        audio = await response.read()
        with wave.open(io.BytesIO(audio)) as wav:
            assert wav.getnframes() == 4000 and wav.getframerate() == 16000
    form = aiohttp.FormData()
    form.add_field("file", audio, filename="sample.wav", content_type="audio/wav")
    form.add_field("response_format", "verbose_json")
    async with client.post(url + "/v1/audio/transcriptions", data=form) as response:
        body = await response.json()
        assert response.status == 200 and body["simulated"]
        assert "WorkerSim" in body["text"] and body["duration"] == 0.25


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,body",
    [
        ("/v1/embeddings", {"input": "example", "dimensions": 1000000}),
        ("/v1/embeddings", {"input": [None]}),
        ("/v1/rerank", {"query": "example", "documents": ["one"], "top_n": -1}),
        ("/v1/audio/speech", {"input": ""}),
    ],
)
async def test_invalid_requests_rejected_before_allocation(engine, path, body):
    client, url, _ = engine
    async with client.post(url + path, json=body) as response:
        assert response.status == 400
