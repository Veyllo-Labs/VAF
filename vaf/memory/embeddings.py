"""
Embedding service for VAF Memory System.

Uses ONNX Runtime (CPU) or sentence-transformers (PyTorch) for text embeddings.
Default: all-MiniLM-L6-v2 (384-dim).

OPTIMIZED: Prefers ONNX Runtime for <200MB RAM usage and <1s startup.
"""

import asyncio
from typing import List, Optional, Dict, Any, Union
from functools import lru_cache
import hashlib
import logging
import os
import time
import numpy as np
from vaf.core.config import Config
from vaf.core.log_helper import append_domain_log

logger = logging.getLogger(__name__)

# Max chars per text sent to the encoder.
MAX_EMBED_INPUT_CHARS = 2512
MAX_EMBED_BATCH_SIZE = 64

# Global model instance with thread-safe loading
import threading
_model = None
_model_name = None
_model_lock = threading.Lock()

class OnnxEmbeddingModel:
    """
    Lightweight ONNX Runtime wrapper for sentence-transformers models.
    Replaces the heavy PyTorch dependency with a ~200MB RAM runtime.
    """
    def __init__(self, model_id: str = "Xenova/all-MiniLM-L6-v2"):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        from huggingface_hub import hf_hub_download

        self.model_id = model_id
        logger.info(f"Initializing ONNX Embedding Model: {model_id}")
        
        # Download resources
        model_path = hf_hub_download(repo_id=model_id, filename="onnx/model_quantized.onnx")
        tokenizer_path = hf_hub_download(repo_id=model_id, filename="tokenizer.json")
        config_path = hf_hub_download(repo_id=model_id, filename="config.json")
        
        # Load Tokenizer
        self.tokenizer = Tokenizer.from_file(tokenizer_path)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", length=512)
        self.tokenizer.enable_truncation(max_length=512)
        
        # Load ONNX Session (CPU) - Memory-leak-safe configuration
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 1  # Single thread for operators
        sess_options.inter_op_num_threads = 1  # Single thread between operators
        sess_options.enable_mem_pattern = False  # Disable memory pattern (reduces memory fragmentation)
        sess_options.enable_cpu_mem_arena = False  # Disable memory arena (prevents memory accumulation)

        self.session = ort.InferenceSession(model_path, sess_options, providers=["CPUExecutionProvider"])
        self.input_names = [i.name for i in self.session.get_inputs()]
        self.output_names = [o.name for o in self.session.get_outputs()]

    def encode(self, sentences: Union[str, List[str]], convert_to_numpy: bool = True, normalize_embeddings: bool = True, show_progress_bar: bool = False) -> Union[List[float], np.ndarray]:
        """Mimics SentenceTransformer.encode"""
        if isinstance(sentences, str):
            sentences = [sentences]
            is_single = True
        else:
            is_single = False
            
        if not sentences:
            return []

        # Tokenize
        encoded = self.tokenizer.encode_batch(sentences)
        
        # Prepare inputs
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        token_type_ids = np.array([e.type_ids for e in encoded], dtype=np.int64)
        
        inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask
        }
        if "token_type_ids" in self.input_names:
            inputs["token_type_ids"] = token_type_ids

        # Run Inference
        outputs = self.session.run(None, inputs)
        
        # Mean Pooling
        # last_hidden_state: [batch, seq, dim]
        last_hidden_state = outputs[0]
        embeddings = self.mean_pooling(last_hidden_state, attention_mask)
        
        # Normalize
        if normalize_embeddings:
            embeddings = embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)
        
        if is_single:
            return embeddings[0]
        return embeddings

    @staticmethod
    def mean_pooling(last_hidden_state: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
        """Perform Mean Pooling (averaging) on the token embeddings."""
        # attention_mask shape: [batch, seq] -> expand to [batch, seq, dim]
        mask_expanded = np.expand_dims(attention_mask, axis=-1).astype(last_hidden_state.dtype)
        
        # Sum embeddings (ignoring padding)
        sum_embeddings = np.sum(last_hidden_state * mask_expanded, axis=1)
        sum_mask = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        
        return sum_embeddings / sum_mask

def get_model():
    """
    Get or load the embedding model (ONNX preferred).

    THREAD-SAFE: Uses lock to prevent multiple threads from loading the model simultaneously.
    This prevents memory leaks from multiple model instances being created.
    """
    global _model, _model_name

    # Default to Xenova's optimized ONNX version of MiniLM
    default_onnx = "Xenova/all-MiniLM-L6-v2"
    config_model = Config.get("memory_embedding_model", "all-MiniLM-L6-v2")

    # If config is default, switch to ONNX ID
    if config_model == "all-MiniLM-L6-v2":
        model_id = default_onnx
        use_onnx = True
    else:
        model_id = config_model
        use_onnx = False  # Fallback to PyTorch for custom models

    # Fast path: model already loaded (no lock needed for read)
    if _model is not None and _model_name == model_id:
        return _model

    # Slow path: need to load model (acquire lock)
    with _model_lock:
        # Double-check after acquiring lock (another thread may have loaded it)
        if _model is not None and _model_name == model_id:
            return _model

        # Log memory BEFORE loading
        mem_before = get_memory_usage_mb()
        logger.info(f"Loading embedding model: {model_id} (Memory before: {mem_before:.0f}MB)")

        try:
            if use_onnx:
                try:
                    _model = OnnxEmbeddingModel(model_id)
                    _model_name = model_id
                    append_domain_log("memory", f"[EMBED] Loaded ONNX {model_id}")
                except Exception as e:
                    logger.warning(f"ONNX load failed ({e}), falling back to PyTorch...")
                    use_onnx = False

            if not use_onnx:
                # Fallback to PyTorch
                os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
                os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:32")
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(model_id, device="cpu")
                _model_name = model_id

            # Log memory AFTER loading
            mem_after = get_memory_usage_mb()
            logger.info(f"Model loaded. Delta: {mem_after-mem_before:.0f}MB")

        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            raise

    return _model


class EmbeddingService:
    """
    Service for generating text embeddings.
    """
    
    CACHE_SIZE = 1000
    
    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or Config.get("memory_embedding_model", "all-MiniLM-L6-v2")
        self._cache: Dict[str, List[float]] = {}
        self._cache_keys: List[str] = []
        self._redis_cache = None
    
    def _get_redis_cache(self):
        if self._redis_cache is None:
            try:
                from vaf.memory.cache import get_cache
                self._redis_cache = get_cache()
            except Exception:
                pass
        return self._redis_cache
    
    @staticmethod
    def _is_e5_model(model_name: str) -> bool:
        return "e5" in (model_name or "").lower()

    @staticmethod
    def _apply_e5_prefix(text: str, prefix: Optional[str]) -> str:
        if not prefix: return text
        if prefix.strip().lower() == "query": return "query: " + text
        if prefix.strip().lower() == "passage": return "passage: " + text
        return text

    def _get_cache_key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _add_to_cache(self, text: str, embedding: List[float]):
        key = self._get_cache_key(text)
        if key in self._cache: return
        if len(self._cache_keys) >= self.CACHE_SIZE:
            old_key = self._cache_keys.pop(0)
            del self._cache[old_key]
        self._cache[key] = embedding
        self._cache_keys.append(key)
    
    def _get_from_cache(self, text: str) -> Optional[List[float]]:
        key = self._get_cache_key(text)
        return self._cache.get(key)
    
    def embed_sync(self, text: str, *, prefix: Optional[str] = None) -> List[float]:
        """
        Generate embedding for a single text synchronously.
        """
        import time
        t0 = time.time()
        
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")

        # --- OPTIMIZATION: SHORT QUERY BYPASS ---
        # Don't embed "Hey", "Hi", "Hello" etc. to avoid waking up the model
        stripped = text.strip()
        if len(stripped) < 5 and not "?" in stripped:
             # Just return a zero vector? No, that breaks cosine sim.
             # Return a random cached vector? No.
             # Actually, we should handle this UPSTREAM in rag.py to skip SEARCH.
             # But here, we just log and proceed.
             pass

        if len(text) > MAX_EMBED_INPUT_CHARS:
            text = text[:MAX_EMBED_INPUT_CHARS].rstrip()
            
        input_text = text
        if self._is_e5_model(self.model_name) and prefix:
            input_text = self._apply_e5_prefix(text, prefix)
        
        cached = self._get_from_cache(input_text)
        if cached:
            append_domain_log("memory", f"[EMBED_HIT] duration={time.time()-t0:.4f}s")
            return cached
            
        append_domain_log("memory", f"[EMBED_MISS] Starting encode for '{input_text[:30]}...'")
        
        t_load_start = time.time()
        model = get_model()
        t_load_end = time.time()
        if t_load_end - t_load_start > 0.1:
             append_domain_log("memory", f"[EMBED_LOAD] Model access took {t_load_end - t_load_start:.4f}s")

        normalize = self._is_e5_model(self.model_name)
        
        t_encode_start = time.time()
        embedding = model.encode(
            input_text,
            convert_to_numpy=True,
            normalize_embeddings=normalize,
        )
        # Handle numpy/list difference
        if hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
            
        t_encode_end = time.time()
        
        duration = t_encode_end - t_encode_start
        append_domain_log("memory", f"[EMBED_DONE] Encode took {duration:.4f}s (Total: {time.time()-t0:.4f}s)")
        
        self._add_to_cache(input_text, embedding)
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        return embedding
    
    def embed_batch_sync(self, texts: List[str], *, prefix: Optional[str] = None) -> List[List[float]]:
        if not texts: return []
        # Reuse single logic for now or implement batch later
        # Since ONNX is fast, loop is fine for small batches
        return [self.embed_sync(t, prefix=prefix) for t in texts]
    
    async def embed(self, text: str, *, prefix: Optional[str] = None) -> List[float]:
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")
        if len(text) > MAX_EMBED_INPUT_CHARS:
            text = text[:MAX_EMBED_INPUT_CHARS].rstrip()
        input_text = self._apply_e5_prefix(text, prefix) if self._is_e5_model(self.model_name) and prefix else text
        
        cached = self._get_from_cache(input_text)
        if cached: return cached
        
        redis_cache = self._get_redis_cache()
        if redis_cache:
            try:
                cached = await redis_cache.get_embedding(input_text)
                if cached:
                    self._add_to_cache(input_text, cached)
                    return cached
            except Exception:
                pass
                
        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(None, lambda: self.embed_sync(text, prefix=prefix))
        
        if redis_cache:
            try:
                asyncio.create_task(redis_cache.set_embedding(input_text, embedding))
            except Exception:
                pass
        return embedding
    
    async def embed_batch(self, texts: List[str], *, prefix: Optional[str] = None) -> List[List[float]]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.embed_batch_sync(texts, prefix=prefix))
    
    def get_dimension(self) -> int:
        dimensions = {
            "all-MiniLM-L6-v2": 384,
            "Xenova/all-MiniLM-L6-v2": 384,
            "intfloat/multilingual-e5-small": 384,
        }
        return dimensions.get(self.model_name, 384)
    
    def clear_cache(self):
        self._cache.clear()
        self._cache_keys.clear()

# Singleton instance
_embedding_service: Optional[EmbeddingService] = None

def get_embedding_service() -> EmbeddingService:
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service

def reset_embedding_service():
    global _embedding_service, _model, _model_name
    _embedding_service = None
    _model = None
    _model_name = None

def cleanup_embedding_memory():
    global _model, _model_name, _embedding_service
    import gc
    if _embedding_service is not None:
        _embedding_service.clear_cache()
    if _model is not None:
        _model = None
        _model_name = None
    gc.collect()

def get_memory_usage_mb() -> float:
    try:
        import psutil
        process = psutil.Process()
        return process.memory_info().rss / (1024 * 1024)
    except ImportError:
        return 0.0

# Text chunking utilities
class TextChunker:
    CHARS_PER_TOKEN = 4
    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 50, min_chunk_size: int = 100):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_size = min_chunk_size
        self.char_chunk_size = chunk_size * self.CHARS_PER_TOKEN
        self.char_overlap = chunk_overlap * self.CHARS_PER_TOKEN
        self.char_min_size = min_chunk_size * self.CHARS_PER_TOKEN
    
    def chunk(self, text: str) -> List[Dict[str, Any]]:
        if not text or len(text) < self.char_min_size:
            return [{"text": text, "start_char": 0, "end_char": len(text), "index": 0}] if text else []
        chunks = []
        start = 0
        index = 0
        while start < len(text):
            end = start + self.char_chunk_size
            if end < len(text):
                search_start = max(start + self.char_min_size, end - 200)
                search_text = text[search_start:end + 100]
                best_break = -1
                for punct in ['. ', '.\n', '! ', '!\n', '? ', '?\n']:
                    idx = search_text.rfind(punct)
                    if idx != -1:
                        potential_break = search_start + idx + len(punct)
                        if potential_break > best_break: best_break = potential_break
                if best_break > start + self.char_min_size: end = best_break
                else:
                    space_idx = text[start:end].rfind(' ')
                    if space_idx > self.char_min_size: end = start + space_idx + 1
            else: end = len(text)
            chunk_text = text[start:end].strip()
            if chunk_text:
                chunks.append({"text": chunk_text, "start_char": start, "end_char": end, "index": index})
                index += 1
            start = end - self.char_overlap
            if start >= len(text): break
        return chunks
    
    def estimate_tokens(self, text: str) -> int:
        return len(text) // self.CHARS_PER_TOKEN

# Singleton chunker instance
_default_chunker: Optional[TextChunker] = None

def get_chunker(chunk_size: Optional[int] = None, chunk_overlap: Optional[int] = None) -> TextChunker:
    """Get or create a TextChunker (singleton for default params to reduce memory)."""
    global _default_chunker

    # Use singleton for default params (most common case)
    if chunk_size is None and chunk_overlap is None:
        if _default_chunker is None:
            _default_chunker = TextChunker(
                chunk_size=Config.get("memory_chunk_size", 512),
                chunk_overlap=Config.get("memory_chunk_overlap", 50)
            )
        return _default_chunker

    # Custom params = new instance
    return TextChunker(
        chunk_size=chunk_size or Config.get("memory_chunk_size", 512),
        chunk_overlap=chunk_overlap or Config.get("memory_chunk_overlap", 50)
    )
