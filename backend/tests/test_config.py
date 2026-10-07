"""生产环境启动自检（``app/core/config.py`` 的 ``_guard_production``）的测试。

这里钉的两条以前都会**静默放行**，属于"看起来配好了、其实没有"的类型：

1. ``PHONE_ENC_KEY`` 的默认值是个**合法的** 32 字节 base64，所以
   "含 dev-only 前缀"和"长度不足"两圈检查都拦不住它。漏配时生产环境会
   用一个公开已知的 AES 密钥加密所有手机号 —— 加密形同虚设。
2. 第一期只有模拟支付渠道，而自检要求生产环境关闭模拟支付。两者由
   ``ALLOW_MOCK_PAYMENT_IN_PROD`` 显式调和；没有它就该启动失败。
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from app.core.config import KNOWN_PHONE_ENC_KEY, MIN_SECRET_BYTES, Settings

# 一组"合格"的生产密钥：长度达标、不含 dev-only 前缀
PROD_SECRET = "x" * MIN_SECRET_BYTES
# 一个和默认值不同的合法密钥
FRESH_ENC_KEY = base64.b64encode(b"y" * 32).decode()


def prod_settings(**overrides: object) -> Settings:
    """构造一份"本该通过"的生产配置，再按需覆盖个别字段。

    显式用 init kwarg 传值：pydantic-settings 里 init 参数的优先级高于
    .env 文件，所以本地那份 backend/.env 不会干扰这些断言。
    """
    base: dict[str, object] = {
        "app_env": "production",
        "jwt_secret": PROD_SECRET,
        "price_token_secret": PROD_SECRET,
        "phone_hash_key": PROD_SECRET,
        "mock_pay_secret": PROD_SECRET,
        "phone_enc_key": FRESH_ENC_KEY,
        "payment_mock_enabled": False,
        # AI 助手的第三方凭据。它不是 HMAC 密钥，所以自检对它只要求"换掉、非空"
        "llm_api_key": PROD_SECRET,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ============================================================
# PHONE_ENC_KEY
# ============================================================
def test_baseline_production_config_passes() -> None:
    """先确认基线本身是过的 —— 否则下面每个断言都可能因为别的原因失败。"""
    assert prod_settings().is_production is True


def test_default_phone_enc_key_is_rejected_in_production() -> None:
    """★ 仓库里的默认值必须被拒。"""
    with pytest.raises(ValidationError) as exc:
        prod_settings(phone_enc_key=KNOWN_PHONE_ENC_KEY)
    assert "PHONE_ENC_KEY" in str(exc.value)


def test_fresh_phone_enc_key_is_accepted() -> None:
    """换成一个新的合法密钥就通过 —— 校验的是"是不是默认值"，不是别的。"""
    assert prod_settings(phone_enc_key=FRESH_ENC_KEY).phone_enc_key == FRESH_ENC_KEY


def test_development_ignores_phone_enc_key_default() -> None:
    """开发环境不做这层校验（否则本地起不来）。"""
    settings = Settings(app_env="development", phone_enc_key=KNOWN_PHONE_ENC_KEY)
    assert settings.is_production is False


# ============================================================
# AI 助手的模型密钥
# ============================================================
def test_missing_llm_key_is_rejected_in_production() -> None:
    """★ 开了助手却没配模型密钥 → 启动就失败。

    不拦的话每次提问都要白跑一趟再失败，而运维只能从日志里看出来。
    """
    with pytest.raises(ValidationError) as exc:
        prod_settings(llm_api_key="dev-only-placeholder")
    assert "LLM_API_KEY" in str(exc.value)


def test_llm_key_is_not_checked_against_the_hmac_length_rule() -> None:
    """★ 它只要求"非空且换掉"，**不套** HS256 那条"至少 32 字节"。

    厂商 API key 不是 HMAC 密钥，套那条规则会让报错文案撒谎（真实密钥
    常常短于 32 字节，会被误报成"太短"）。
    """
    short_key = "sk-short-but-real"
    assert prod_settings(llm_api_key=short_key).llm_api_key == short_key


def test_assistant_disabled_needs_no_llm_key() -> None:
    """没开助手，就不该逼运维配一个用不上的密钥。"""
    settings = prod_settings(assistant_enabled=False, llm_api_key="dev-only-placeholder")
    assert settings.assistant_enabled is False


# ============================================================
# 模拟支付
# ============================================================
def test_mock_payment_requires_explicit_optin() -> None:
    """★ 生产环境开模拟支付，必须有显式授权，否则拒绝启动。"""
    with pytest.raises(ValidationError) as exc:
        prod_settings(payment_mock_enabled=True)
    assert "ALLOW_MOCK_PAYMENT_IN_PROD" in str(exc.value)


def test_mock_payment_allowed_with_optin() -> None:
    """演示环境走的就是这条路：production + 显式允许模拟支付。"""
    settings = prod_settings(payment_mock_enabled=True, allow_mock_payment_in_prod=True)
    assert settings.payment_mock_enabled is True
    assert settings.allow_mock_payment_in_prod is True


def test_optin_alone_does_not_force_mock_on() -> None:
    """授权开着但没开模拟支付，是接真实渠道时的正常状态，不该报错。"""
    settings = prod_settings(payment_mock_enabled=False, allow_mock_payment_in_prod=True)
    assert settings.payment_mock_enabled is False


# ============================================================
# 既有的两条检查（防止被上面的改动带坏）
# ============================================================
def test_dev_placeholder_secret_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        prod_settings(jwt_secret="dev-only-jwt-secret-change-me-32bytes-min")
    assert "JWT_SECRET" in str(exc.value)


def test_short_secret_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        prod_settings(jwt_secret="too-short")
    assert "JWT_SECRET" in str(exc.value)
