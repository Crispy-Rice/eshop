"""手机号等敏感字段的加解密。

设计见 docs/13-schema.md §2。三份数据各司其职：

======================  ============================================
``phone_hash``          HMAC-SHA256，**确定性**，用于登录查询与唯一约束
``phone_cipher``        AES-256-GCM，用于客服查看原文
``phone_masked``        138****8888，列表展示用，不需要解密
======================  ============================================

为什么不用可逆加密做查询：加密结果随机化（带 nonce），密文每次都不一样，
没法建唯一索引也没法 `WHERE phone = ?`。所以查询必须靠确定性哈希。

AES-GCM 是认证加密：密文被篡改会在解密时抛 ``InvalidTag``，而不是返回
一段看似正常的垃圾数据。每个密文用独立的随机 nonce，nonce 拼在密文前面。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

NONCE_BYTES = 12  # GCM 推荐 96 bit

KEY_VERSION = 1  # 预留：将来轮换密钥时用来标记密文是用哪一版密钥加的


def _hash_key() -> bytes:
    return get_settings().phone_hash_key.encode()


def _phone_aesgcm() -> AESGCM:
    return AESGCM(base64.b64decode(get_settings().phone_enc_key))


def phone_hash(phone: str) -> str:
    """确定性哈希，用于登录查询与唯一约束。

    ★ 换 ``PHONE_HASH_KEY`` 会导致所有存量用户无法登录（哈希对不上），
    所以这个密钥必须长期保存。
    """
    return hmac.new(_hash_key(), phone.encode("utf-8"), hashlib.sha256).hexdigest()


def phone_encrypt(phone: str) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + _phone_aesgcm().encrypt(nonce, phone.encode("utf-8"), None)


def phone_decrypt(cipher: bytes) -> str:
    """解密。``PHONE_ENC_KEY`` 丢失或换过会导致历史密文无法解密。"""
    nonce, body = cipher[:NONCE_BYTES], cipher[NONCE_BYTES:]
    return _phone_aesgcm().decrypt(nonce, body, None).decode("utf-8")


def mask_phone(phone: str) -> str:
    """138****8888。长度不足时全部打码，避免泄露短号。"""
    if len(phone) < 7:
        return "*" * len(phone)
    return f"{phone[:3]}****{phone[-4:]}"


def mask_name(name: str) -> str:
    """张三 → 张*，张小三 → 张*三。收货人姓名展示用。"""
    if len(name) <= 1:
        return name
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]
