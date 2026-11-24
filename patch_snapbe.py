#!/usr/bin/env python3
"""Snap only: park the stop at entry after +0.30R, and cut at -0.85R."""
import os
import re
import shutil
import py_compile

TARGET = "main.py"
BACKUP = "main.py.bak_snapbe"

ARM = '''async def arm_snap_entry_stop(symbol: str, trade: dict, entry_price: float, stop_dist: float, side: str) -> None:
    """After +0.30R, park a Snap stop at entry and keep the 0.70R target."""
    if not trade.get("snap") or trade.get("snap_be"):
        return
    peak = float(trade.get("peak_r") or 0)
    if peak < float(getattr(config, "SNAP_BE_PEAK_R", 0.30)):
        return
    if entry_price <= 0 or side not in ("BUY", "SELL"):
        return
    cd = trade.get("sl_replace_cooldown_until")
    if cd is not None and now_utc() < cd:
        return
    if trade.get("_be_busy"):
        return
    trade["_be_busy"] = True
    try:
        price = await adjust_price(entry_price, symbol)
        order = await replace_close_position_stop(symbol, side, price)
        if not order:
            trade["sl_replace_cooldown_until"] = now_utc() + timedelta(seconds=20)
            return
        trade["snap_be"] = True
        trade["breakeven_moved"] = True
        trade["arm_stop_moved"] = True
        logger.info(f"🛡️ Snap BE {symbol} @ {price} | peak={peak:.2f} → SL entry")
        await send_telegram(f"🛡️ Snap BE {symbol} | peak={peak:.2f} → SL entry")
        qty = float(trade.get("qty") or 0)
        if stop_dist > 0 and qty > 0:
            tgt_r = float(getattr(config, "SNAP_TARGET_R", 0.70))
            tgt = entry_price + stop_dist * tgt_r if side == "BUY" else entry_price - stop_dist * tgt_r
            try:
                tp_px = await adjust_price(tgt, symbol)
                tp_qty = await adjust_quantity(symbol, qty)
                if tp_px > 0 and tp_qty > 0:
                    await client.futures_create_order(
                        symbol=symbol,
                        side="SELL" if side == "BUY" else "BUY",
                        type="TAKE_PROFIT_MARKET",
                        stopPrice=tp_px,
                        quantity=tp_qty,
                        reduceOnly=True,
                        workingType="CONTRACT_PRICE",
                    )
                    logger.info(f"🎯 SNAP target kept {symbol} @ {tp_px}")
            except Exception as e:
                logger.warning(f"SNAP target rearm failed {symbol} | {e}")
    finally:
        trade["_be_busy"] = False


'''


def fail(msg):
    shutil.copy2(BACKUP, TARGET)
    print(msg)
    print(f"restored from {BACKUP} — bot file was not changed")


def replace_n(text, old, new, label, nmin, nmax):
    n = text.count(old)
    if n < nmin or n > nmax:
        return text, f"{label} count={n}"
    return text.replace(old, new), None


def main():
    if not os.path.exists(TARGET):
        print(f"STOPPED missing {TARGET}")
        return
    shutil.copy2(TARGET, BACKUP)
    text = open(TARGET, encoding="utf-8").read().replace("\r\n", "\n").replace("\r", "\n")
    if "async def arm_snap_entry_stop(" in text or "SNAP_BE_PEAK_R" in text:
        fail("STOPPED snap breakeven is already in this file")
        return

    cfg = re.compile(r"^(    SNAP_MAX_HOLD_SEC = [0-9.]+)\n", re.M)
    found = list(cfg.finditer(text))
    if len(found) != 1:
        fail(f"STOPPED snap-config count={len(found)}")
        return
    text = text[:found[0].end()] + (
        "    SNAP_BE_PEAK_R = 0.30\n"
        "    SNAP_MAX_LOSS_R = -0.85\n"
    ) + text[found[0].end():]

    pairs = [
        (
            "def snap_exit_reason(trade: dict, current_r: float, hold_min: float):\n"
            "    if not trade.get(\"snap\"):\n"
            "        return None\n"
            "    if current_r >= float(getattr(config, \"SNAP_TARGET_R\", 0.70)):\n",
            ARM
            + "def snap_exit_reason(trade: dict, current_r: float, hold_min: float):\n"
            "    if not trade.get(\"snap\"):\n"
            "        return None\n"
            "    if current_r <= float(getattr(config, \"SNAP_MAX_LOSS_R\", -0.85)):\n"
            "        return f\"SNAP_CUT R={current_r:.2f}\"\n"
            "    if trade.get(\"snap_be\") and current_r <= 0.0:\n"
            "        return f\"SNAP_BE R={current_r:.2f}\"\n"
            "    if current_r >= float(getattr(config, \"SNAP_TARGET_R\", 0.70)):\n",
            "exit",
            1,
            1,
        ),
        (
            "                        snap_reason = snap_exit_reason(trade, current_r, hold_time)\n",
            "                        if trade.get(\"snap\"):\n"
            "                            trade[\"peak_r\"] = max(float(trade.get(\"peak_r\") or current_r), current_r)\n"
            "                            try:\n"
            "                                await arm_snap_entry_stop(symbol, trade, entry_price, stop_dist, side)\n"
            "                            except Exception:\n"
            "                                pass\n"
            "                        snap_reason = snap_exit_reason(trade, current_r, hold_time)\n",
            "monitor",
            1,
            4,
        ),
        (
            "            logger.info(f\"📏 Exchange SL {symbol} | ${cut_usd:.2f} cap | dist={sl_dist:.8g} wide={wide_dist:.8g}\")\n",
            "            if (quality_report or {}).get(\"snap\") and stop_dist > 0:\n"
            "                snap_cap = stop_dist * abs(float(getattr(config, \"SNAP_MAX_LOSS_R\", -0.85)))\n"
            "                if 0 < snap_cap < sl_dist:\n"
            "                    sl_dist = snap_cap\n"
            "                    emergency_sl_price = await adjust_price(\n"
            "                        entry_price - sl_dist if side == \"BUY\" else entry_price + sl_dist, symbol\n"
            "                    )\n"
            "                    logger.info(f\"📏 SNAP stop {symbol} | 0.85R @ {emergency_sl_price}\")\n"
            "            logger.info(f\"📏 Exchange SL {symbol} | ${cut_usd:.2f} cap | dist={sl_dist:.8g} wide={wide_dist:.8g}\")\n",
            "stop",
            1,
            1,
        ),
    ]
    for old, new, label, nmin, nmax in pairs:
        text, err = replace_n(text, old, new, label, nmin, nmax)
        if err:
            fail(f"STOPPED {err}")
            return
    if "SNAP_CUT R=" not in text or "async def arm_snap_entry_stop(" not in text:
        fail("STOPPED snap breakeven was not installed")
        return
    open(TARGET, "w", encoding="utf-8").write(text)
    try:
        py_compile.compile(TARGET, doraise=True)
    except Exception as e:
        fail(f"STOPPED compile: {e}")
        return
    print(f"DONE pieces={len(pairs) + 1}")
    print(f"backup={BACKUP}")


if __name__ == "__main__":
    main()
