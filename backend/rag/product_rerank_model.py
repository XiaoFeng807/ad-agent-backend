"""The pinned BGE model shared by the R2 replay and product chat."""

import platform
from hashlib import file_digest
from pathlib import Path
from time import perf_counter

MODEL = "BAAI/bge-reranker-v2-m3"
REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
WEIGHT_SHA256 = "d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286"
BATCH_SIZE = 16
MAX_LENGTH = 512


class Reranker:
    def __init__(self, device="cuda"):
        start = perf_counter()
        import torch
        import transformers
        from huggingface_hub import try_to_load_from_cache

        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA requested but unavailable in this PyTorch environment")
        path = try_to_load_from_cache(MODEL, "model.safetensors", revision=REVISION)
        if not isinstance(path, str):
            raise RuntimeError("Missing fixed model weights in HF_HOME")
        self.weight_sha256 = digest(Path(path))
        if self.weight_sha256 != WEIGHT_SHA256:
            raise RuntimeError("Unexpected model weights")
        self.verify_ms = elapsed(start)
        start = perf_counter()
        self.torch = torch
        self.device = torch.device("cuda:0" if device == "cuda" else "cpu")
        dtype = torch.float16 if self.device.type == "cuda" else torch.float32
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(MODEL, revision=REVISION, local_files_only=True)
        self.model = transformers.AutoModelForSequenceClassification.from_pretrained(
            MODEL, revision=REVISION, local_files_only=True, use_safetensors=True, trust_remote_code=False,
        ).to(device=self.device, dtype=dtype).eval()
        if self.device.type == "cuda":
            torch.cuda.synchronize(self.device)
        self.metadata = {
            "id": MODEL, "revision": REVISION, "weight_sha256": self.weight_sha256,
            "device": str(next(self.model.parameters()).device), "processor": platform.processor(),
            "dtype": str(dtype).removeprefix("torch."), "retrieval_device": "cpu",
            "threads": torch.get_num_threads(), "interop_threads": torch.get_num_interop_threads(),
            "batch_size": BATCH_SIZE, "max_length": MAX_LENGTH,
            "torch": torch.__version__, "transformers": transformers.__version__,
            "python": platform.python_version(), "platform": platform.platform(),
            "weight_verification_ms": self.verify_ms, "local_load_ms": elapsed(start),
        }
        if self.device.type == "cuda":
            properties = torch.cuda.get_device_properties(self.device)
            self.metadata.update(gpu=properties.name, cuda=torch.version.cuda, torch_cuda_arches=torch.cuda.get_arch_list(),
                                 compute_capability=[properties.major, properties.minor],
                                 gpu_memory_bytes=properties.total_memory)
        self.calls = 0

    def score(self, query, passages):
        self.calls += 1
        try:
            return self._score(query, passages)
        except Exception:
            if self.device.type == "cuda":
                self.torch.cuda.synchronize(self.device)
            raise

    def _score(self, query, passages):
        scores = []
        with self.torch.inference_mode():
            for start in range(0, len(passages), BATCH_SIZE):
                batch = passages[start:start + BATCH_SIZE]
                inputs = self.tokenizer([query] * len(batch), batch, padding=True, truncation=True,
                                        max_length=MAX_LENGTH, return_tensors="pt").to(self.device)
                scores.append(self.model(**inputs).logits.flatten())
        # The copy waits for both batches to finish, including all CUDA work in the measurement.
        return self.torch.cat(scores).float().cpu().tolist()


def digest(path):
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest()


def elapsed(start):
    return (perf_counter() - start) * 1000


