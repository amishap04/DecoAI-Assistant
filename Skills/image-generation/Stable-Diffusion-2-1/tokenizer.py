"""CLIP tokenizer for the Stable Diffusion 2.1 text encoder.

text_encoder.bin expects `tokens` shaped (1, 77) with dtype int32 (see metadata.json).
"""

import numpy as np
from tokenizers import Tokenizer

TOKENIZER_MAX_LENGTH = 77
PAD_TOKEN_ID = 49407

_tokenizer = Tokenizer.from_pretrained("openai/clip-vit-large-patch14")
_tokenizer.enable_truncation(TOKENIZER_MAX_LENGTH)
_tokenizer.enable_padding(pad_id=PAD_TOKEN_ID, length=TOKENIZER_MAX_LENGTH)


def run_tokenizer(prompt):
    token_ids = _tokenizer.encode(prompt).ids
    return np.array(token_ids, dtype=np.int32).reshape(1, TOKENIZER_MAX_LENGTH)
