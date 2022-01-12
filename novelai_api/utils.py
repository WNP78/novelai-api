from argon2 import low_level
from nacl.secret import SecretBox
from nacl.exceptions import CryptoError

from hashlib import blake2b
from base64 import urlsafe_b64encode, b64encode, b64decode
import json
from zlib import decompress as inflate, compressobj as deflate_obj, MAX_WBITS, Z_BEST_COMPRESSION
from transformers import GPT2Tokenizer

from novelai_api.Keystore import Keystore

from typing import Dict, Union, List, Tuple, Any, Optional, Iterable, NoReturn

def argon_hash(email: str, password: str, size: int, domain: str) -> str:
    pre_salt = password[:6] + email + domain

    # salt
    blake = blake2b(digest_size = 16)
    blake.update(pre_salt.encode())
    salt = blake.digest()

    raw = low_level.hash_secret_raw(password.encode(), salt, 2, int(2000000/1024), 1, size, low_level.Type.ID)
    hashed = urlsafe_b64encode(raw).decode()

    return hashed

def get_access_key(email: str, password: str) -> str:
    return argon_hash(email, password, 64, "novelai_data_access_key")[:64]

def get_encryption_key(email: str, password: str) -> bytes:
    pre_key = argon_hash(email, password, 128, "novelai_data_encryption_key")
    pre_key = pre_key.replace('=', '')

    blake = blake2b(digest_size = 32)
    blake.update(pre_key.encode())
    return blake.digest()

def decrypt_data(data: Union[str, bytes], key: bytes, nonce: Optional[bytes] = None) -> Union[Tuple[str, bytes, bool], Tuple[None, None, bool]]:
    box = SecretBox(key)

    if type(data) is not bytes:
        data = data.encode()

    # data is compressed
    is_compressed = (len(data) >= 16 and data[:16] == b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01")
    if is_compressed:
        data = data[16:]

    if nonce is None:
        nonce = data[:box.NONCE_SIZE]
        data = data[box.NONCE_SIZE:]

    try:
        data = box.decrypt(data, nonce = nonce)
        if is_compressed:
            data = inflate(data, -MAX_WBITS)

        return (data.decode(), nonce, is_compressed)
    except CryptoError:
        return (None, None, False)

def encrypt_data(data: Union[str, bytes], key: bytes, nonce: Optional[bytes] = None, is_compressed: bool = False) -> bytes:
    box = SecretBox(key)

    if type(data) is not bytes:
        data = data.encode()

    # FIXME: zlib results in different data than the library used by NAI, but they are fully compatible
    if is_compressed:
        deflater = deflate_obj(Z_BEST_COMPRESSION, wbits = -MAX_WBITS)
        data = deflater.compress(data) + deflater.flush()

    data = bytes(box.encrypt(data, nonce))

    if is_compressed:
        data = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01" + data

    return data

# function injection to avoid circular import
if not hasattr(Keystore, "_encrypt_data"):
    Keystore._encrypt_data = encrypt_data

if not hasattr(Keystore, "_decrypt_data"):
    Keystore._decrypt_data = decrypt_data

def decompress_user_data(items: Union[List[Dict[str, Any]], Dict[str, Any]]) -> NoReturn:
    """
    Decompress the data of each item in :ref: items
    Doesn't decrypt, but does a b64 to UTF8 translation
    """

    if type(items) is not list and type(items) is not tuple:
        items = [items]

    for item in items:
        assert type(item) is dict, f"Expected type 'dict' for item of 'items', got type '{type(item)}'"
        assert "data" in item, f"Expected key 'data' in item"

        try:
            item["data"] = json.loads(b64decode(item["data"]).decode())
            item["decrypted"] = True    # not decrypted, per say, but for genericity
        except json.JSONDecodeError:
            item["decrypted"] = False

def compress_user_data(items: Union[List[Dict[str, Any]], Dict[str, Any]]) -> NoReturn:
    """
    Compress the data of each item in :ref: items
    Doesn't encrypt, but does a UTF8 to b64 translation
    Must have been decompressed by decompress_user_data()
    """

    if type(items) is not list and type(items) is not tuple:
        items = [items]

    for item in items:
        assert type(item) is dict, f"Expected type 'dict' for item of 'items', got type '{type(item)}'"
        assert "data" in item, f"Expected key 'data' in item"

        if "decrypted" in item:
            if item["decrypted"]:
                item["data"] = b64encode(json.dumps(item["data"], separators = (',', ':')).encode()).decode()
            del item["decrypted"]

def decrypt_user_data(items: Union[List[Dict[str, Any]], Dict[str, Any]], keystore: Keystore) -> NoReturn:
    """
    Decrypt the data of each item in :ref: items
    If a item has already been decrypted, it won't be decrypted a second time

    :param items: Item or list of items to decrypt
    :param keystore: Keystore retrieved with the get_keystore method
    """

    # 1 item
    if type(items) is not list and type(items) is not tuple:
        items = [items]

    for item in items:
        assert type(item) is dict, f"Expected type 'dict' for item of 'items', got type '{type(item)}'"

        if not item.get("decrypted", False):
            # FIXME: replace the assert by meaningful errors ? Expect the data to be right ?
            assert "data" in item, f"Expected key 'data' in item"
            assert "meta" in item, f"Expected key 'meta' in item"

            meta = item["meta"]
#            assert meta in keystore["keys"]
            if meta not in keystore:
                print("Meta missing:", meta)
            else:
                key = keystore[meta]

                data, nonce, is_compressed = decrypt_data(b64decode(item["data"]), key)
                if data is not None:
                    try:
                        data = json.loads(data)
                        item["data"] = data
                        item["nonce"] = nonce
                        item["decrypted"] = True
                        item["compressed"] = is_compressed
                        continue

                    except json.JSONDecodeError:
                        pass

            item["decrypted"] = False

def encrypt_user_data(items: Union[List[Dict[str, Any]], Dict[str, Any]], keystore: Keystore) -> NoReturn:
    """
    Encrypt the data of each item in :ref: items
    If a item has already been encrypted, it won't be encrypted a second time
    Must have been decrypted by decrypt_user_data()

    :param items: Item or list of items to encrypt
    :param keystore: Keystore retrieved with the get_keystore method
    """

    # 1 item
    if type(items) is not list and type(items) is not tuple:
        items = [items]

    for item in items:
        assert type(item) is dict, f"Expected type 'dict' for item of 'items', got type '{type(item)}'"

        if "decrypted" in item:
            if item["decrypted"]:
                # FIXME: replace the assert by meaningful errors ? Expect the data to be right ?
                assert "data" in item, f"Expected key 'data' in item"
                assert "meta" in item, f"Expected key 'meta' in item"
                assert "nonce" in item, f"Expected key 'nonce' in item"
                assert "compressed" in item, f"Expected key 'compressed' in item"

                meta = item["meta"]
    #            assert meta in keystore["keys"]
                if meta not in keystore:
                    print("Meta missing:", meta)
                else:
                    key = keystore[meta]

                    data = json.dumps(item["data"], separators = (',', ':'))
                    data = b64encode(encrypt_data(data, key, item["nonce"], item["compressed"])).decode()

                    item["data"] = data
                    del item["nonce"]
                    del item["compressed"]

            del item["decrypted"]

def map_meta_to_stories(stories: Union[List[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Dict[str, Union[str, int]]]:
    data = {}
    for story in stories:
        data[story["meta"]] = story

    return data

def assign_content_to_story(stories: Dict[str, Dict[str, Union[str, int]]], story_contents: Union[List[Dict[str, Any]], Dict[str, Any]]) -> NoReturn:
    assert type(stories) is dict, "Stories must be mapped, before being associated with their content"

    if type(story_contents) is not list and type(story_contents) is not tuple:
        story_contents = [story_contents]

    for story_content in story_contents:
        meta = story_content["meta"]

        if meta in stories and story_content["decrypted"] and stories[meta]["decrypted"]:
            stories[meta]["content"] = story_content

def remove_non_decrypted_user_data(items: List[Dict[str, Any]]) -> NoReturn:
    for i in range(len(items)):
        if items[i].get("decrypted", False) is False:
            items.pop(i)
            i -= 1

tokenizer = None

def tokens_to_b64(tokens: Iterable[int]) -> str:
    return b64encode(t.to_bytes(2, "little") for t in tokens).decode()

def b64_to_tokens(b64: str) -> List[int]:
    b = b64decode(b64)

    return list(b[i:i + 2] for i in range(0, len(b), 2))

def tokens_to_text(tokens: List[int]) -> str:
    global tokenizer

    if tokenizer is None:   # lazy initialization, as tokenizer is heavy
        tokenizer = GPT2Tokenizer.from_pretrained("gpt2")

    tokenizer.decode(tokens)

# TODO: story tree builder