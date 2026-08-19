"""Red boxes inside screenshots name the field a procedure step points at.

The manuals burn red rectangles into their screenshots to say which button
or field a step refers to — raster, not vector, so only image analysis sees
them. The OCR'd contents become `emphasis_screen` metadata and lexical
terms, never cited text (the same INV-1 rule as text highlights).

The geometry (HSV mask at both hue ends of red, dilate, external contours,
size floor) is hermetic — cv2 ships with docling. Only the final OCR step
needs the RapidOCR weights, so that test is opt-in behind the existing
Docling integration gate.
"""

import os
from pathlib import Path

import numpy as np
import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Modifier,
    SparseVectorParams,
    VectorParams,
)

from docquery.config import Settings
from docquery.ingest.chunker import Chunk
from docquery.ingest.image_emphasis import (
    _ocr_crop,
    attach_screen_emphasis,
    find_red_boxes,
)
from docquery.ingest.loader import Document, load_document

FIXTURES = Path(__file__).parent / "fixtures"


def _canvas(w: int = 400, h: int = 300) -> np.ndarray:
    return np.full((h, w, 3), 230, dtype=np.uint8)


def _outline(img: np.ndarray, x: int, y: int, w: int, h: int, color, thickness=3):
    img[y : y + thickness, x : x + w] = color
    img[y + h - thickness : y + h, x : x + w] = color
    img[y : y + h, x : x + thickness] = color
    img[y : y + h, x + w - thickness : x + w] = color


def test_a_red_rectangle_border_is_found():
    img = _canvas()
    _outline(img, 50, 40, 200, 80, (200, 0, 0))

    boxes = find_red_boxes(img)

    assert len(boxes) == 1
    x, y, w, h = boxes[0]
    # The 3x3 dilate grows the box by a pixel or two; the region must match.
    assert abs(x - 50) <= 3 and abs(y - 40) <= 3
    assert abs(w - 200) <= 6 and abs(h - 80) <= 6


def test_boxes_below_the_size_floor_are_discarded():
    """Specks and thin decorations are not marks around a field."""
    img = _canvas()
    _outline(img, 100, 100, 20, 8, (200, 0, 0), thickness=2)

    assert find_red_boxes(img) == []


def test_both_hue_ends_of_red_are_caught():
    """Red wraps around the HSV hue axis: pure red sits near 0, crimson near
    180 — a single range would miss half the corpus's markers."""
    img = _canvas(800, 300)
    _outline(img, 40, 40, 200, 80, (200, 0, 0))  # hue ~0
    _outline(img, 400, 40, 200, 80, (180, 0, 40))  # hue ~170

    assert len(find_red_boxes(img)) == 2


def test_the_inner_crop_excludes_the_red_border():
    """The border itself must not reach OCR: it reads as noise glyphs."""
    img = _canvas()
    _outline(img, 50, 40, 200, 80, (200, 0, 0), thickness=3)
    seen: list[np.ndarray] = []

    class Engine:
        def __call__(self, crop):
            seen.append(crop)

            class Result:
                txts = ("Categoria",)

            return Result()

    text = _ocr_crop(img, (50, 40, 200, 80), Engine())

    assert text == "Categoria"
    crop = seen[0]
    red_mask = (crop[:, :, 0] > 150) & (crop[:, :, 1] < 80) & (crop[:, :, 2] < 80)
    assert not red_mask.any()
    # Upscaled 2x from the (w-8, h-8) inner crop.
    assert crop.shape[0] == (80 - 8) * 2 and crop.shape[1] == (200 - 8) * 2


def test_screen_emphasis_maps_by_page_with_a_document_level_fallback():
    doc = Document(content="", metadata={})
    doc.emphasis_screen = {1: ["Categoria"], 2: ["BLOQUEIO DE CONTA"]}
    paged = [
        Chunk(text="p1", metadata={"page_number": 1}),
        Chunk(text="p2", metadata={"page_number": 2}),
    ]
    attach_screen_emphasis(doc, paged)
    assert paged[0].metadata["emphasis_screen"] == ["Categoria"]
    assert paged[1].metadata["emphasis_screen"] == ["BLOQUEIO DE CONTA"]

    flat = [Chunk(text="a", metadata={})]
    attach_screen_emphasis(doc, flat)
    assert flat[0].metadata["emphasis_screen"] == ["Categoria", "BLOQUEIO DE CONTA"]


def test_flag_off_changes_nothing():
    doc = load_document(FIXTURES / "highlighted.pdf", Settings())
    assert doc.emphasis_screen is None


# --- Integration: terms join the sparse index, never the stored text --------

DIM = 8
COLLECTION = "test_image_emphasis"


def _fake_dense(texts: list[str], **_kwargs) -> np.ndarray:
    return np.tile(np.eye(1, DIM, dtype=np.float32), (len(texts), 1))


def test_screen_emphasis_joins_the_lexical_index_but_never_the_text(monkeypatch):
    from docquery.ingest import pipeline
    from docquery.ingest.sparse import _stable_hash

    monkeypatch.setattr(pipeline, "embed_texts", _fake_dense)
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
    )
    passage = "Preencha o formulario e envie o chamado."
    chunk = Chunk(
        text=passage,
        metadata={
            "source": "docs/manual.pdf",
            "chunk_index": 0,
            "emphasis_screen": ["BLOQUEIO DE CONTA"],
        },
    )

    pipeline.ingest_chunks(
        [chunk],
        client,
        Settings(
            qdrant_collection=COLLECTION,
            embedding_model="test/fake-embedder",
            embedding_dimension=DIM,
        ),
    )

    points, _ = client.scroll(
        collection_name=COLLECTION, limit=10, with_payload=True, with_vectors=True
    )
    payload = points[0].payload
    assert payload["text"] == passage
    assert payload["emphasis_screen"] == ["BLOQUEIO DE CONTA"]
    assert _stable_hash("bloqueio") in points[0].vector["sparse"].indices


# --- Opt-in: real OCR over the committed screenshot fixture -----------------


@pytest.mark.docling
@pytest.mark.skipif(
    os.environ.get("DOCQUERY_DOCLING_INTEGRATION") != "1",
    reason="set DOCQUERY_DOCLING_INTEGRATION=1 to run RapidOCR over the fixture",
)
def test_the_red_boxed_string_is_extracted_from_the_fixture():
    from docquery.ingest.image_emphasis import extract_screen_emphasis

    settings = Settings(image_emphasis_enabled=True)
    found = extract_screen_emphasis(FIXTURES / "screenshot_redbox.pdf", settings)

    assert 1 in found
    assert any("4711" in text for text in found[1])
