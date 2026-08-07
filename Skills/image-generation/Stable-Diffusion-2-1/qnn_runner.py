"""Client-side wrapper around the persistent ORT QNN session server.

Handles the manual quantize/dequantize step this model package requires
(its wrapper .onnx files expose raw uint16 I/O, unlike SD3's fp32-wrapped
export) so callers of run_text_encoder/run_unet/run_vae only ever see plain
float32/uint8 arrays.
"""

from multiprocessing.managers import BaseManager

import numpy as np

from quantization import (
    TEXT_EMBEDDING_SCALE, TEXT_EMBEDDING_ZP,
    UNET_LATENT_SCALE, UNET_LATENT_ZP,
    UNET_TIMESTEP_SCALE, UNET_TIMESTEP_ZP,
    UNET_TEXT_EMB_SCALE, UNET_TEXT_EMB_ZP,
    UNET_OUTPUT_LATENT_SCALE, UNET_OUTPUT_LATENT_ZP,
    VAE_LATENT_SCALE, VAE_LATENT_ZP,
    VAE_IMAGE_SCALE, VAE_IMAGE_ZP,
    quantize, dequantize,
)
from server_config import SERVER_HOST, SERVER_PORT, SERVER_AUTHKEY

TEXT_EMBEDDING_SHAPE = (1, 77, 1024)
LATENT_SHAPE = (1, 64, 64, 4)
IMAGE_SHAPE = (1, 512, 512, 3)


class _SessionManager(BaseManager):
    pass


_SessionManager.register("SessionStore")

_manager = None
_store = None


def _get_store():
    global _manager, _store
    if _manager is None:
        _manager = _SessionManager(address=(SERVER_HOST, SERVER_PORT), authkey=SERVER_AUTHKEY)
        _manager.connect()
        print(f"[client] Connected to session server at {SERVER_HOST}:{SERVER_PORT}", flush=True)
        _store = _manager.SessionStore()
    return _store


def run_text_encoder(token_ids):
    """int32 [1,77] -> dequantized float32 [1,77,1024]."""
    store = _get_store()
    outputs = store.run_session("text_encoder", {"tokens": np.ascontiguousarray(token_ids, dtype=np.int32)})
    text_embedding_q = outputs[0]
    return dequantize(text_embedding_q, TEXT_EMBEDDING_SCALE, TEXT_EMBEDDING_ZP).reshape(TEXT_EMBEDDING_SHAPE)


def run_unet(latent, timestep, text_embedding):
    """latent [1,64,64,4] NHWC, scalar timestep, text_embedding [1,77,1024] (all float) ->
    dequantized float32 [1,64,64,4] NHWC.
    """
    store = _get_store()
    feeds = {
        "latent": quantize(latent, UNET_LATENT_SCALE, UNET_LATENT_ZP),
        "timestep": quantize(np.full((1, 1), timestep), UNET_TIMESTEP_SCALE, UNET_TIMESTEP_ZP),
        "text_emb": quantize(text_embedding, UNET_TEXT_EMB_SCALE, UNET_TEXT_EMB_ZP),
    }
    outputs = store.run_session("unet", feeds)
    output_latent_q = outputs[0]
    return dequantize(output_latent_q, UNET_OUTPUT_LATENT_SCALE, UNET_OUTPUT_LATENT_ZP).reshape(LATENT_SHAPE)


def run_vae(latent):
    """latent [1,64,64,4] NHWC float32 -> uint8 RGB image [512,512,3]."""
    store = _get_store()
    feeds = {"latent": quantize(latent, VAE_LATENT_SCALE, VAE_LATENT_ZP)}
    outputs = store.run_session("vae", feeds)
    image_q = outputs[0]
    image = dequantize(image_q, VAE_IMAGE_SCALE, VAE_IMAGE_ZP)
    return np.clip(image * 255.0, 0, 255).astype(np.uint8).reshape(IMAGE_SHAPE[1:])
