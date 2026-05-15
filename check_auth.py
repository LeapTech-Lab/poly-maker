"""打印 Polymarket 认证相关配置，帮助排查 invalid signature。

不会发送订单，也不会打印私钥。
"""

from __future__ import annotations

from eth_account import Account

from config import BotConfig


def mask(value: str) -> str:
    if not value:
        return "<empty>"
    return f"{value[:6]}...{value[-4:]}"


def main() -> None:
    config = BotConfig()
    signer = Account.from_key(config.private_key).address if config.private_key else ""
    funder = config.funder_address

    print("Signer address from PK:", mask(signer))
    print("FUNDER_ADDRESS:", mask(funder))
    print("BROWSER/FUNDER same as signer:", signer.lower() == funder.lower() if signer and funder else False)
    print("SIGNATURE_TYPE:", config.signature_type)
    print()
    print("Signature type reference:")
    print("0 = EOA / MetaMask direct wallet, funder usually equals signer")
    print("1 = POLY_PROXY / existing Polymarket proxy wallet, common for email/Google login")
    print("2 = GNOSIS_SAFE / existing Safe flow")
    print("3 = POLY_1271 / deposit wallet flow for new API users")
    print()
    print("If orders fail with invalid signature, the usual fix is SIGNATURE_TYPE/FUNDER_ADDRESS.")
    print("Check Polymarket settings for the profile/deposit wallet address, then update .env.")


if __name__ == "__main__":
    main()
