"""GPU OCR for scanned papers, on your own Modal account.

A scanned PDF has no text layer, so PyMuPDF returns nothing and the paper
cannot be narrated at all. This endpoint reads page images and returns WORDS
WITH BOXES — not just text — because the read-along view highlights each word
as it is spoken, so geometry is not optional. That rules out the markdown/LaTeX
converters (Nougat, Marker) and vision LLMs, which return prose without boxes.

docTR (Mindee, Apache-2.0) keeps the repo unambiguously MIT. Surya is stronger
on academic layout but is GPL-3.0, which would relicense anything that imports
it.

Deploy:  modal deploy modal_ocr_app.py     # prints the endpoint URL
then in config.toml:

    [ocr]
    enabled = true
    modal_endpoint = "https://<you>--rhapsode-ocr-doctrocr-ocr.modal.run"
    modal_token_id = "..."
    modal_token_secret = "..."
"""

import modal

# Set True to require Modal proxy-auth tokens on the endpoint (recommended
# if you mind strangers who guess the URL spending your credits).
REQUIRES_PROXY_AUTH = True
# Pages per request. Modal enforces a hard 150 s HTTP timeout on web endpoints
# regardless of `timeout=` below, so keep one request well inside it; the
# client sends more batches.
MAX_PAGES = 4

app = modal.App("rhapsode-ocr")


def _bake_weights():
    # fetch the detection/recognition weights at image-build time so a
    # scale-from-zero cold start does not re-download them every time
    from doctr.models import ocr_predictor
    ocr_predictor(det_arch="db_resnet50", reco_arch="crnn_vgg16_bn",
                  pretrained=True)


image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libgl1", "libglib2.0-0")     # OpenCV runtime deps
    .pip_install("python-doctr[torch]>=0.8", "torch", "torchvision", "pillow")
    .run_function(_bake_weights)
    # kept last: appending a layer reuses the cached weight bake above
    .pip_install("fastapi[standard]")
)


@app.cls(image=image, gpu="T4", scaledown_window=120, timeout=600)
class DoctrOCR:
    @modal.enter()
    def load(self):
        from doctr.models import ocr_predictor
        self.model = ocr_predictor(det_arch="db_resnet50",
                                   reco_arch="crnn_vgg16_bn",
                                   pretrained=True).cuda()

    @modal.fastapi_endpoint(method="POST",
                            requires_proxy_auth=REQUIRES_PROXY_AUTH)
    def ocr(self, req: dict) -> dict:
        """{"pages": [base64 PNG, ...]} -> per page, words with RELATIVE boxes.

        Boxes come back normalised to 0..1 of the page image, so the client can
        map them onto the PDF's own coordinate space whatever DPI it rendered
        at — the read-along rectangles then line up with the page images the
        viewer shows."""
        import base64

        from doctr.io import DocumentFile

        pages = req.get("pages") or []
        if not pages:
            return {"error": "no pages", "pages": []}
        if len(pages) > MAX_PAGES:
            return {"error": f"max {MAX_PAGES} pages per request (Modal's "
                             f"150 s web-endpoint timeout); send more batches",
                    "pages": []}
        try:
            images = [base64.b64decode(p) for p in pages]
        except Exception as e:
            return {"error": f"bad base64: {e}", "pages": []}

        result = self.model(DocumentFile.from_images(images))
        out = []
        for page in result.export()["pages"]:
            words = []
            for block in page.get("blocks", []):
                for line in block.get("lines", []):
                    for w in line.get("words", []):
                        (x0, y0), (x1, y1) = w["geometry"]
                        text = (w.get("value") or "").strip()
                        if text:
                            words.append({"text": text,
                                          "bbox": [x0, y0, x1, y1],
                                          "conf": round(w.get("confidence", 0), 3)})
            out.append({"words": words})
        return {"pages": out}
