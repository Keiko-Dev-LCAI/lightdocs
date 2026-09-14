"""Shared KEIKO payment verifier for the Orca / Light app portfolio.

Adds a "Pay with KEIKO" rail alongside the existing LCAI / USDC paywalls.

KEIKO is a standard ERC-20 on Lightchain AI (Chain ID 9200):
  contract  0x93ed20e33e7c88cfa73348086ed1f2c7a2b50854
  decimals  18
  supply    1,000,000,000

How it works (mirrors the proven synthetic-USDC path in the OrcaVault relay):
the user sends KEIKO from THEIR OWN wallet to the app's receiving wallet, then
hands the app the transaction hash. This module confirms on-chain that the
token contract emitted a Transfer of >= the required amount, from the payer, to
the receiving wallet, inside that transaction. If so, the payment is good.

Design notes:
  * Verification only. This module never holds a private key and never signs or
    sends anything. The payment is made by the user's own wallet.
  * Stdlib only (urllib + json). No web3 dependency, so it drops into any Python
    backend in the portfolio (relay server, binai, orcaapp-server, ...).
  * Replay protection: a used-tx store makes sure one payment can't unlock more
    than one thing. Important for per-purchase apps (tips, per-item unlocks).
  * Pricing is a flat KEIKO amount per unlock, set per app. To run a
    "pay in KEIKO and save 20%" promo, just set the KEIKO price ~20% below the
    LCAI/USDC price for the same item.
"""

import json
import os
import threading
import urllib.request
from decimal import Decimal

# ── Config (env-overridable, safe defaults) ──────────────────────────────────
KEIKO_TOKEN_ADDRESS = os.environ.get(
    "KEIKO_TOKEN_ADDRESS", "0x93ed20e33e7c88cfa73348086ed1f2c7a2b50854"
).strip().lower()
KEIKO_DECIMALS = int(os.environ.get("KEIKO_DECIMALS", "18"))
KEIKO_RPC_URL = os.environ.get("KEIKO_RPC_URL", "https://rpc.mainnet.lightchain.ai").strip()
# Wallet that receives KEIKO payments. MUST be set per deployment (public address).
KEIKO_RECEIVE_WALLET = os.environ.get("KEIKO_RECEIVE_WALLET", "").strip().lower()
# Default price per unlock, in whole KEIKO. Override per call or via env.
KEIKO_PRICE = float(os.environ.get("KEIKO_PRICE", "10000"))

# keccak256("Transfer(address,address,uint256)")
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _rpc(method, params, rpc_url=None, timeout=15):
    """Minimal JSON-RPC call. Returns the 'result' field or raises."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        rpc_url or KEIKO_RPC_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.load(resp)
    if "error" in out and out["error"]:
        raise RuntimeError(out["error"])
    return out.get("result")


def _norm_addr(a):
    return (a or "").strip().lower()


def _hx(x):
    """Normalize an RPC hex value (topic/data) to lowercase 0x-string."""
    h = x if isinstance(x, str) else str(x)
    h = h.lower()
    return h if h.startswith("0x") else "0x" + h


def _to_units(amount, decimals):
    """Whole KEIKO -> integer base-units, EXACTLY (no float rounding).

    Float math loses precision at 18 decimals with large amounts (e.g. 500M),
    so use Decimal. Accepts int / str / float.
    """
    return int(Decimal(str(amount)) * (Decimal(10) ** int(decimals)))


def _fmt_amount(amount):
    """Human-friendly amount without scientific notation."""
    d = Decimal(str(amount))
    return format(d.normalize(), "f")


def verify_keiko_transfer(
    tx_hash,
    from_wallet,
    to_wallet,
    min_amount_keiko,
    token_address=None,
    decimals=None,
    rpc_url=None,
):
    """Confirm a KEIKO Transfer happened in `tx_hash`.

    Requires: a Transfer log emitted by the KEIKO token contract, FROM
    `from_wallet` TO `to_wallet`, for value >= `min_amount_keiko` whole KEIKO.

    Returns (ok: bool, err: str | None). Never raises for on-chain conditions;
    only returns ok=False with a human-readable reason.
    """
    token = _norm_addr(token_address or KEIKO_TOKEN_ADDRESS)
    dec = KEIKO_DECIMALS if decimals is None else int(decimals)
    want_from = _norm_addr(from_wallet)
    want_to = _norm_addr(to_wallet)

    if not token:
        return False, "KEIKO token address is not configured"
    if not want_to:
        return False, "KEIKO receiving wallet is not configured"
    if not tx_hash or not str(tx_hash).startswith("0x"):
        return False, "A valid transaction hash is required"

    need_units = _to_units(min_amount_keiko, dec)

    try:
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash], rpc_url)
    except Exception:
        return False, "Could not reach the chain — wait a moment and try again"
    if receipt is None:
        return False, "Transaction not yet mined — wait a moment and try again"
    # status is "0x1" success / "0x0" failed (may be absent on very old chains)
    status = receipt.get("status")
    if status is not None and int(status, 16) == 0:
        return False, "Transaction failed on chain"

    for log in receipt.get("logs", []):
        try:
            if _norm_addr(log.get("address")) != token:
                continue
            topics = log.get("topics", [])
            if len(topics) < 3 or _hx(topics[0]) != _TRANSFER_TOPIC:
                continue
            from_addr = "0x" + _hx(topics[1])[-40:]
            to_addr = "0x" + _hx(topics[2])[-40:]
            value = int(_hx(log.get("data", "0x0")), 16)
            if from_addr == want_from and to_addr == want_to and value >= need_units:
                return True, None
        except Exception:
            continue

    return False, (
        f"No KEIKO transfer of >= {_fmt_amount(min_amount_keiko)} KEIKO to the "
        f"receiving wallet was found in this transaction"
    )


class UsedTxStore:
    """Tiny file-backed set of already-consumed tx hashes (replay protection).

    Thread-safe for a single process. For multi-process deployments, back this
    with the app's existing datastore instead — the interface is just
    `.seen(tx)` and `.add(tx)`.
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._set = set()
        try:
            with open(path, "r") as fh:
                self._set = {ln.strip().lower() for ln in fh if ln.strip()}
        except FileNotFoundError:
            pass

    def seen(self, tx):
        return _norm_addr(tx) in self._set

    def add(self, tx):
        with self._lock:
            self._set.add(_norm_addr(tx))
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write("\n".join(sorted(self._set)))
            os.replace(tmp, self.path)


def register_keiko_payment(
    tx_hash,
    from_wallet,
    to_wallet=None,
    amount_keiko=None,
    used_tx_store=None,
    token_address=None,
    decimals=None,
    rpc_url=None,
):
    """Full one-call check for an app endpoint.

    Verifies the transfer AND (if a store is given) that this tx hasn't already
    been redeemed. On success, records the tx as used and returns ok=True.

    Returns (ok: bool, err: str | None).
    """
    to_wallet = to_wallet or KEIKO_RECEIVE_WALLET
    amount_keiko = KEIKO_PRICE if amount_keiko is None else amount_keiko

    if used_tx_store is not None and used_tx_store.seen(tx_hash):
        return False, "This payment has already been used"

    ok, err = verify_keiko_transfer(
        tx_hash, from_wallet, to_wallet, amount_keiko,
        token_address=token_address, decimals=decimals, rpc_url=rpc_url,
    )
    if not ok:
        return False, err

    if used_tx_store is not None:
        used_tx_store.add(tx_hash)
    return True, None
