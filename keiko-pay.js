/*
 * keiko-pay.js — shared "Pay with KEIKO" frontend helper for the Orca/Light apps.
 *
 * Sends a KEIKO (ERC-20) transfer from the user's own wallet (MetaMask /
 * injected provider, incl. a Ledger via MetaMask) to the app's receiving
 * wallet, then hands the tx hash to the app's verify endpoint. No secrets,
 * no private keys — the user signs in their own wallet.
 *
 * KEIKO on Lightchain AI: chainId 9200, 18 decimals,
 * token 0x93ed20e33e7c88cfa73348086ed1f2c7a2b50854.
 *
 * Usage:
 *   const res = await payWithKeiko({
 *     to:        '0xYourReceivingWallet',
 *     amount:    10000,                 // whole KEIKO
 *     verifyUrl: '/api/keiko/verify',   // your backend endpoint
 *     meta:      { item: 'pro-unlock' } // optional, passed to backend
 *   });
 *   if (res.ok) { ...unlock... }
 *   else        { alert(res.error); }
 */
(function (global) {
  "use strict";

  var KEIKO = {
    token: "0x93ed20e33e7c88cfa73348086ed1f2c7a2b50854",
    decimals: 18,
    chainIdHex: "0x23f0", // 9200
    // transfer(address,uint256)
    transferSelector: "0xa9059cbb",
  };

  function pad64(hexNoPrefix) {
    return hexNoPrefix.toLowerCase().replace(/^0x/, "").padStart(64, "0");
  }

  // amount (whole KEIKO, may be fractional) -> integer base-units hex, 18 decimals.
  // Uses BigInt string math to avoid float rounding on large numbers.
  function toUnitsHex(amountWhole, decimals) {
    var s = String(amountWhole);
    var neg = s[0] === "-";
    if (neg) throw new Error("amount must be positive");
    var parts = s.split(".");
    var intPart = parts[0] || "0";
    var fracPart = (parts[1] || "").slice(0, decimals);
    while (fracPart.length < decimals) fracPart += "0";
    var units = BigInt(intPart) * (10n ** BigInt(decimals)) + BigInt(fracPart || "0");
    return units.toString(16);
  }

  function buildTransferData(to, amountWhole) {
    var toClean = to.toLowerCase().replace(/^0x/, "");
    var amtHex = toUnitsHex(amountWhole, KEIKO.decimals);
    return KEIKO.transferSelector + pad64(toClean) + pad64(amtHex);
  }

  async function ensureChain(provider) {
    try {
      var current = await provider.request({ method: "eth_chainId" });
      if (current && current.toLowerCase() === KEIKO.chainIdHex) return;
      await provider.request({
        method: "wallet_switchEthereumChain",
        params: [{ chainId: KEIKO.chainIdHex }],
      });
    } catch (e) {
      // Non-fatal: let the send attempt surface a clear error if still wrong.
    }
  }

  async function payWithKeiko(opts) {
    opts = opts || {};
    var provider = global.ethereum;
    if (!provider) {
      return { ok: false, error: "No wallet found. Install/enable MetaMask." };
    }
    if (!opts.to) return { ok: false, error: "Missing receiving wallet." };
    if (!opts.amount) return { ok: false, error: "Missing KEIKO amount." };

    try {
      var accounts = await provider.request({ method: "eth_requestAccounts" });
      var from = (accounts && accounts[0]) || null;
      if (!from) return { ok: false, error: "No wallet account connected." };

      await ensureChain(provider);

      var data = buildTransferData(opts.to, opts.amount);
      // Ledger-via-MetaMask note: user must confirm on the device; if it errors,
      // enable "blind signing"/contract data in the Ledger Ethereum app.
      var txHash = await provider.request({
        method: "eth_sendTransaction",
        params: [{ from: from, to: KEIKO.token, data: data, value: "0x0" }],
      });

      if (!opts.verifyUrl) {
        // No backend verification requested — just return the hash.
        return { ok: true, txHash: txHash, from: from };
      }

      var body = {
        walletAddress: from,
        txHash: txHash,
        amount: opts.amount,
        meta: opts.meta || null,
      };
      var resp = await fetch(opts.verifyUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      var out = await resp.json().catch(function () { return {}; });
      if (!resp.ok || out.error) {
        return { ok: false, error: out.error || "Payment could not be verified", txHash: txHash };
      }
      return { ok: true, txHash: txHash, from: from, result: out };
    } catch (e) {
      var msg = (e && e.message) || "Payment cancelled or failed.";
      return { ok: false, error: msg };
    }
  }

  global.KeikoPay = { pay: payWithKeiko, config: KEIKO, buildTransferData: buildTransferData };
  if (typeof module !== "undefined" && module.exports) module.exports = global.KeikoPay;
})(typeof window !== "undefined" ? window : this);
