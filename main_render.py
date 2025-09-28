# ====================================================================================
# Apex BOT v8.9 - Final Stability Release (Render ReadTimeout/Stability 対策)
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
import yfinance as yf
import asyncio
import random
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse 
import uvicorn
from dotenv import load_dotenv
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 📌 初期監視対象銘柄リスト 
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE"]

YFINANCE_SUPPORTED_SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "ADA", "XRP", "LTC", "BCH"]

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

LOOP_INTERVAL = 45      # メイン分析ループ間隔 (45秒)
DYNAMIC_UPDATE_INTERVAL = 600 # マクロ分析/銘柄更新間隔 (10分)
REQUEST_DELAY = 0.5     # CCXTリクエスト間の遅延 (0.5秒)
PING_TIMEOUT = 30       # Pingタイムアウトを30秒に維持
PING_INTERVAL = 30      # 📌 v8.9 修正: Renderのアイドル対策としてPing間隔を30秒に短縮

# ログ設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    force=True)

logging.getLogger('ccxt').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# グローバル状態変数
CCXT_CLIENTS_DICT: Dict[str, ccxt_async.Exchange] = {}
CCXT_CLIENT_NAMES: List[str] = []
CCXT_CLIENT_NAME: str = 'Initializing'
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS
NOTIFIED_SYMBOLS: Dict[str, float] = {}
NEUTRAL_NOTIFIED_TIME: float = 0
LAST_SUCCESS_TIME: float = 0.0
TOTAL_ANALYSIS_ATTEMPTS: int = 0
TOTAL_ANALYSIS_ERRORS: int = 0
ACTIVE_CLIENT_HEALTH: Dict[str, float] = {} 


# ====================================================================================
# UTILITIES & CLIENTS
# ====================================================================================

def initialize_ccxt_client():
    """CCXTクライアントを初期化（複数クライアントで負荷分散）"""
    global CCXT_CLIENTS_DICT, CCXT_CLIENT_NAMES, ACTIVE_CLIENT_HEALTH

    clients = {
        'Binance': ccxt_async.binance({"enableRateLimit": True, "timeout": 20000}),
        'Bybit': ccxt_async.bybit({"enableRateLimit": True, "timeout": 30000}), 
        'OKX': ccxt_async.okx({"enableRateLimit": True, "timeout": 30000}),     
        'Coinbase': ccxt_async.coinbase({"enableRateLimit": True, "timeout": 20000,
                                         "options": {"defaultType": "spot", "fetchTicker": "public"}}),
    }

    CCXT_CLIENTS_DICT = clients
    CCXT_CLIENT_NAMES = list(CCXT_CLIENTS_DICT.keys())
    ACTIVE_CLIENT_HEALTH = {name: time.time() for name in CCXT_CLIENT_NAMES}


async def send_test_message():
    """起動テスト通知 (v8.9に更新)"""
    test_text = (
        f"🤖 <b>Apex BOT v8.9 - 起動テスト通知</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"<b>Render ReadTimeout対策のため、Ping間隔を30秒に短縮しました。</b>"
    )
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: send_telegram_html(test_text, is_emergency=True))
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

# (send_telegram_html, get_tradfi_macro_context, get_news_sentiment, 
#  calculate_elliott_wave_score, calculate_trade_levels, 
#  calculate_technical_indicators, get_ml_prediction, 
#  fetch_ohlcv_single_client, fetch_order_book_depth_async, 
#  format_telegram_message の関数は v8.8と同一のため省略)

def send_telegram_html(text: str, is_emergency: bool = False):
    if 'YOUR' in TELEGRAM_TOKEN:
        clean_text = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("<pre>", "\n").replace("</pre>", "")
        logging.warning("⚠️ TELEGRAM_TOKENが初期値です。ログに出力されます。")
        logging.info("--- TELEGRAM通知（ダミー）---\n" + clean_text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True, "disable_notification": not is_emergency
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logging.info(f"✅ Telegram通知成功。Response Status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram送信エラーが発生しました: {e}")

def get_tradfi_macro_context() -> Dict: 
    context = {"trend": "不明", "vix_level": 0.0, "gvix_level": 0.0}
    try:
        vix = yf.Ticker("^VIX").history(period="1d", interval="1h")
        if not vix.empty:
            context["vix_level"] = vix['Close'].iloc[-1]
            context["trend"] = "中立" if context["vix_level"] < 20 else "リスクオフ (VIX高)"
        context["gvix_level"] = random.uniform(40, 60)
    except Exception:
        pass
    return context

def get_news_sentiment(symbol: str) -> Dict: 
    sentiment_score = 0.5
    if random.random() < 0.1: sentiment_score = 0.65
    elif random.random() > 0.9: sentiment_score = 0.35
    return {"sentiment_score": sentiment_score}

def calculate_elliott_wave_score(closes: pd.Series) -> Tuple[float, str]:
    if len(closes) < 50: return 0.0, "不明"
    volatility = closes.pct_change().std()
    recent_trend_strength = closes.iloc[-1] / closes.iloc[-20:].mean() - 1
    if volatility < 0.005 and abs(recent_trend_strength) < 0.01:
        wave_score = 0.2
        wave_phase = "修正波 (レンジ)"
    elif abs(recent_trend_strength) > 0.05 and volatility > 0.01:
        wave_score = 0.8
        wave_phase = "推進波 (トレンド)"
    else:
        wave_score = random.uniform(0.3, 0.7)
        wave_phase = "移行期"
    return wave_score, wave_phase

def calculate_trade_levels(closes: pd.Series, side: str, score: float) -> Dict:
    if len(closes) < 20:
        current_price = closes.iloc[-1]
        return {"entry": current_price, "sl": current_price, "tp1": current_price, "tp2": current_price}
    current_price = closes.iloc[-1]
    volatility_range = closes.diff().abs().std() * 2
    multiplier = 1.0 + score * 0.5
    if side == "ロング":
        entry = current_price * 0.9995
        sl = current_price - (volatility_range * 1.0)
        tp1 = current_price + (volatility_range * 1.5 * multiplier)
        tp2 = current_price + (volatility_range * 3.0 * multiplier)
    else:
        entry = current_price * 1.0005
        sl = current_price + (volatility_range * 1.0)
        tp1 = current_price - (volatility_range * 1.5 * multiplier)
        tp2 = current_price - (volatility_range * 3.0 * multiplier)
    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2}

def calculate_technical_indicators(ohlcv: List[list]) -> Dict:
    closes = pd.Series([c[4] for c in ohlcv])
    if len(closes) < 50:
        return {"rsi": 50, "macd_hist": 0, "macd_direction_boost": 0, "adx": 25, "cci_signal": 0}

    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs)).iloc[-1]

    exp1 = closes.ewm(span=12, adjust=False).mean()
    exp2 = closes.ewm(span=26, adjust=False).mean()
    macd = exp1 - exp2
    signal_line = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - signal_line
    macd_direction_boost = 0.0
    if macd_hist.iloc[-1] > 0 and macd_hist.iloc[-2] <= 0: macd_direction_boost = 0.05
    elif macd_hist.iloc[-1] < 0 and macd_hist.iloc[-2] >= 0: macd_direction_boost = -0.05
    elif macd_hist.iloc[-1] > 0: macd_direction_boost = 0.02
    elif macd_hist.iloc[-1] < 0: macd_direction_boost = -0.02

    highs = pd.Series([c[2] for c in ohlcv])
    lows = pd.Series([c[3] for c in ohlcv])
    adx = ((highs - lows).abs() / closes).rolling(window=14).mean().iloc[-1] * 1000
    adx = np.clip(adx, 15, 60)

    tp = (highs + lows + closes) / 3
    ma = tp.rolling(window=20).mean()
    md = tp.rolling(window=20).apply(lambda x: abs(x - x.mean()).mean(), raw=True)
    cci = (tp - ma) / (0.015 * md)

    return {
        "rsi": rsi,
        "macd_hist": macd_hist.iloc[-1],
        "macd_direction_boost": macd_direction_boost,
        "adx": adx,
        "cci_signal": cci.iloc[-1]
    }

def get_ml_prediction(ohlcv: List[list], sentiment: Dict) -> Tuple[float, Dict]:
    try:
        tech_data = calculate_technical_indicators(ohlcv)
        rsi = tech_data["rsi"]
        macd_boost = tech_data["macd_direction_boost"]
        cci = tech_data["cci_signal"]

        news_sentiment = sentiment.get("sentiment_score", 0.5)
        base_prob = 0.5 + ((rsi - 50) / 100) * 0.5
        cci_boost = np.clip(cci / 500, -0.05, 0.05) * -1

        final_prob = base_prob + macd_boost + (news_sentiment - 0.5) * 0.1 + cci_boost
        win_prob = np.clip(final_prob, 0.35, 0.65)
        return win_prob, tech_data

    except Exception:
        return 0.5, {"rsi": 50, "macd_hist": 0, "macd_direction_boost": 0, "adx": 25, "cci_signal": 0}

async def fetch_ohlcv_single_client(client_name: str, symbol: str, timeframe: str, limit: int) -> Tuple[List[list], str]:
    client = CCXT_CLIENTS_DICT.get(client_name)
    if client is None: return [], "NoClient"

    trial_symbols = [f"{symbol}/USDT"]
    if client_name == 'Coinbase': trial_symbols.insert(0, f"{symbol}-USD")
    elif client_name == 'Upbit': trial_symbols.insert(0, f"{symbol}/KRW")
    elif client_name == 'Binance': trial_symbols.append(f"{symbol}/BUSD")

    for market_symbol in trial_symbols:
        try:
            await asyncio.sleep(REQUEST_DELAY) 
            ohlcv = await client.fetch_ohlcv(market_symbol, timeframe, limit=limit)
            if ohlcv and len(ohlcv) >= limit:
                global ACTIVE_CLIENT_HEALTH
                ACTIVE_CLIENT_HEALTH[client_name] = time.time() 
                return ohlcv, "Success"

        except ccxt_async.RateLimitExceeded:
            logging.warning(f"⚠️ CCXT ({client_name}, {market_symbol}) RateLimitExceeded。即時クライアント切り替え要求。")
            return [], "RateLimit" 
        except ccxt_async.RequestTimeout:
            logging.warning(f"⚠️ CCXT ({client_name}, {market_symbol}) RequestTimeout。即時クライアント切り替え要求。")
            return [], "Timeout" 
        except ccxt_async.BadSymbol:
            logging.debug(f"ℹ️ CCXT ({client_name}, {market_symbol}) BadSymbol。次のペアを試行。")
            continue
        except (ccxt_async.ExchangeError, ccxt_async.NetworkError) as e:
            logging.debug(f"ℹ️ CCXT ({client_name}, {market_symbol}) Error: {type(e).__name__}。次のペアを試行。")
            continue
        except Exception:
            continue

    return [], "NoData"

async def fetch_order_book_depth_async(symbol: str) -> Dict:
    client = CCXT_CLIENTS_DICT.get(CCXT_CLIENT_NAME)
    if client is None: return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5}

    market_symbol = f"{symbol}/USDT" 

    try:
        await asyncio.sleep(REQUEST_DELAY)
        order_book = await client.fetch_order_book(market_symbol, limit=20)
        bid_volume = sum(amount * price for price, amount in order_book['bids'][:5])
        ask_volume = sum(amount * price for price, amount in order_book['asks'][:5])

        total_volume = bid_volume + ask_volume
        depth_ratio = bid_volume / total_volume if total_volume > 0 else 0.5

        return {"bid_volume": bid_volume, "ask_volume": ask_volume, "depth_ratio": depth_ratio}

    except Exception:
        return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5}

async def update_monitor_symbols_dynamically(client_name: str, limit: int = 30) -> None:
    """動的銘柄選定 (v8.8ロジックを維持: TypeErrorを確実に回避するためのフィルタリング強化)"""
    global CURRENT_MONITOR_SYMBOLS
    client = CCXT_CLIENTS_DICT.get(client_name)
    if client is None: return

    try:
        markets = await client.load_markets()
        usdt_pairs = {
            symbol: market_data for symbol, market_data in markets.items()
            if 'USDT' in symbol and market_data.get('active', True)
        }

        target_symbols = list(usdt_pairs.keys())
        if len(target_symbols) > 150: 
            target_symbols = random.sample(target_symbols, 150)

        tickers = await client.fetch_tickers(target_symbols)
        
        # v8.8 フィルタリング: quoteVolumeがNoneや負の数を返す可能性のある銘柄をフィルタリング
        valid_tickers = [
            t for t in tickers.values() if 
            t.get('quoteVolume') is not None and 
            t.get('quoteVolume', 0) > 0 and
            t.get('symbol') is not None and 
            'USDT' in t['symbol']
        ]

        # 出来高 (quote volume) でソート
        sorted_tickers = sorted(
            valid_tickers,
            key=lambda x: x['quoteVolume'],
            reverse=True
        )

        new_symbols = [t['symbol'].split('/')[0] for t in sorted_tickers][:limit]

        if len(new_symbols) > 5:
            CURRENT_MONITOR_SYMBOLS = list(set(new_symbols))
            logging.info(f"✅ 動的銘柄選定成功。{client_name}のTOP{len(new_symbols)}銘柄を監視対象に設定。")
        else:
            logging.warning(f"⚠️ 動的銘柄選定に失敗 (銘柄数不足: {len(new_symbols)}), デフォルトリストを維持。")

    except Exception as e:
        logging.error(f"❌ 動的銘柄選定エラー: {type(e).__name__}: {e}。既存リスト({len(CURRENT_MONITOR_SYMBOLS)}銘柄)を維持。")
        


async def generate_signal_candidate(symbol: str, macro_context_data: Dict, client_name: str) -> Optional[Dict]:
    """シグナル候補を生成 (v8.8と同じロジックを維持)"""
    sentiment_data = get_news_sentiment(symbol)
    ohlcv_15m, ccxt_status = await fetch_ohlcv_single_client(client_name, symbol, '15m', 100)

    if ccxt_status in ["RateLimit", "Timeout"]:
        return {"symbol": symbol, "side": ccxt_status, "score": 0.0, "client": client_name}

    is_fallback = False
    closes: Optional[pd.Series] = None
    win_prob = 0.5
    tech_data: Dict[str, Any] = {}
    source = client_name

    if ccxt_status == "Success":
        win_prob, tech_data = get_ml_prediction(ohlcv_15m, sentiment_data)
        closes = pd.Series([c[4] for c in ohlcv_15m])
        wave_score, wave_phase = calculate_elliott_wave_score(closes)
    elif symbol in YFINANCE_SUPPORTED_SYMBOLS:
        return None 
    else:
        return None

    if closes is None: return None

    depth_data = await fetch_order_book_depth_async(symbol) if not is_fallback else {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5}

    if win_prob >= 0.53:
        side = "ロング"
    elif win_prob <= 0.47:
        side = "ショート"
    else:
        confidence = abs(win_prob - 0.5)
        regime = "レンジ相場"
        return {"symbol": symbol, "side": "Neutral", "confidence": confidence, "regime": regime,
                "macro_context": macro_context_data, "is_fallback": is_fallback,
                "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio'],
                "tech_data": tech_data, "sentiment_score": sentiment_data["sentiment_score"]}

    base_score = abs(win_prob - 0.5) * 2
    base_score *= (0.8 + wave_score * 0.4)

    adx_level = tech_data.get('adx', 25)
    if adx_level > 30: adx_boost = 0.1
    elif adx_level < 20: 
        base_score *= 0.8
        adx_boost = 0.0
    else: adx_boost = 0.0

    sentiment_boost = (sentiment_data["sentiment_score"] - 0.5) * 0.2
    if side == "ショート": sentiment_boost *= -1

    depth_adjustment = (depth_data['depth_ratio'] - 0.5) * 0.2 if side == "ロング" else (0.5 - depth_data['depth_ratio']) * 0.2

    vix_penalty = 1.0
    if macro_context_data['vix_level'] > 25: vix_penalty = 0.8

    final_score = np.clip((base_score + depth_adjustment + adx_boost + sentiment_boost) * vix_penalty, 0.0, 1.0)

    trade_levels = calculate_trade_levels(closes, side, final_score)

    return {"symbol": symbol, "side": side, "price": closes.iloc[-1], "score": final_score,
            "entry": trade_levels['entry'], "sl": trade_levels['sl'],
            "tp1": trade_levels['tp1'], "tp2": trade_levels['tp2'],
            "regime": "トレンド相場", "is_fallback": is_fallback,
            "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio'],
            "vix_level": macro_context_data['vix_level'], "macro_context": macro_context_data,
            "source": source, "sentiment_score": sentiment_data["sentiment_score"],
            "tech_data": tech_data}


async def self_ping_task(interval: int = PING_INTERVAL):
    """
    v8.9 修正: Ping間隔を短縮 (30秒)し、成功ログを追加。
    """
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if not render_url:
        logging.warning("⚠️ RENDER_EXTERNAL_URL環境変数が設定されていません。自己Pingは無効です。")
        return
    logging.info(f"🟢 自己Pingタスクを開始します (インターバル: {interval}秒, T/O: {PING_TIMEOUT}秒)。URL: {render_url}")
    if not render_url.startswith('http'):
        render_url = f"https://{render_url}"
        
    ping_url = render_url.rstrip('/') + '/' 
    
    while True:
        await asyncio.sleep(interval)
        try:
            # タイムアウトをPING_TIMEOUTに設定 (30秒)
            response = requests.get(ping_url, timeout=PING_TIMEOUT)
            response.raise_for_status()
            # 📌 v8.9 修正: 成功時のログを追加
            logging.debug(f"✅ Self-ping successful. Status: {response.status_code}")
        except requests.exceptions.RequestException as e:
            logging.warning(f"❌ Self-ping failed ({type(e).__name__}): {e}. Retrying.")
            # 失敗時は、次のPingまでの待機時間を少し長く取る (Renderのリカバリを待つ)
            await asyncio.sleep(5) 
        except asyncio.CancelledError:
            break

async def main_loop():
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS, NOTIFIED_SYMBOLS, NEUTRAL_NOTIFIED_TIME
    global LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    global CCXT_CLIENT_NAME, ACTIVE_CLIENT_HEALTH

    loop = asyncio.get_event_loop()
    macro_context_data = await loop.run_in_executor(None, get_tradfi_macro_context)
    LAST_UPDATE_TIME = time.time()
    await send_test_message()
    # 📌 v8.9 修正: PING_INTERVAL (30秒)でタスクを開始
    asyncio.create_task(self_ping_task(interval=PING_INTERVAL))

    while True:
        try:
            current_time = time.time()

            CCXT_CLIENT_NAME = max(ACTIVE_CLIENT_HEALTH, key=ACTIVE_CLIENT_HEALTH.get, default=CCXT_CLIENT_NAMES[0])
            logging.debug(f"現在の優先クライアント: {CCXT_CLIENT_NAME}")

            # --- 動的更新フェーズ (10分に一度) ---
            if (current_time - LAST_UPDATE_TIME) >= DYNAMIC_UPDATE_INTERVAL:
                logging.info("==================================================")
                logging.info(f"Apex BOT v8.9 分析サイクル開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
                macro_context_data = await loop.run_in_executor(None, get_tradfi_macro_context)
                await update_monitor_symbols_dynamically(CCXT_CLIENT_NAME)
                LAST_UPDATE_TIME = current_time
                logging.info(f"監視銘柄数: {len(CURRENT_MONITOR_SYMBOLS)}")
                logging.info("--------------------------------------------------")
            else:
                logging.info(f"🔍 分析開始 (データソース: {CCXT_CLIENT_NAME})")

            # --- メイン分析実行 ---
            candidate_tasks = [generate_signal_candidate(sym, macro_context_data, CCXT_CLIENT_NAME)
                               for sym in CURRENT_MONITOR_SYMBOLS]
            candidates = await asyncio.gather(*candidate_tasks)

            rate_limit_error_found = any(isinstance(c, dict) and c.get('side') in ["RateLimit", "Timeout"] for c in candidates)

            if rate_limit_error_found:
                penalized_time = time.time() - 3600
                logging.error(f"❌ レート制限/タイムアウトエラー発生: クライアント {CCXT_CLIENT_NAME} のヘルスを {penalized_time:.0f} にリセット。")
                ACTIVE_CLIENT_HEALTH[CCXT_CLIENT_NAME] = penalized_time
                next_client = max(ACTIVE_CLIENT_HEALTH, key=ACTIVE_CLIENT_HEALTH.get, default=CCXT_CLIENT_NAMES[0])
                logging.info(f"➡️ 即時クライアント切り替え：次回は {next_client} を優先試行。")
                
                await asyncio.sleep(5) 
                continue 

            valid_candidates_and_neutral = [c for c in candidates if c is not None and c.get('side') not in ["RateLimit", "Timeout"]]
            success_count = len(valid_candidates_and_neutral)
            TOTAL_ANALYSIS_ATTEMPTS += len(CURRENT_MONITOR_SYMBOLS)
            TOTAL_ANALYSIS_ERRORS += len(CURRENT_MONITOR_SYMBOLS) - success_count
            if success_count > 0: LAST_SUCCESS_TIME = current_time

            valid_candidates = [c for c in valid_candidates_and_neutral if c.get('side') != "Neutral" and c.get('score', 0) >= 0.50]
            neutral_candidates = [c for c in valid_candidates_and_neutral if c.get('side') == "Neutral"]

            if valid_candidates:
                best_signal = max(valid_candidates, key=lambda c: c['score'])
                is_not_recently_notified = current_time - NOTIFIED_SYMBOLS.get(best_signal['symbol'], 0) > 3600
                if is_not_recently_notified:
                    message = format_telegram_message(best_signal)
                    await loop.run_in_executor(None, lambda: send_telegram_html(message, is_emergency=True))
                    NOTIFIED_SYMBOLS[best_signal['symbol']] = current_time
                    logging.info(f"🔔 最優秀候補: {best_signal['symbol']} - {best_signal['side']} (スコア: {best_signal['score']:.4f}) | 通知実行")

            time_since_last_neutral = current_time - NEUTRAL_NOTIFIED_TIME
            is_neutral_notify_due = time_since_last_neutral > 1800

            if is_neutral_notify_due:
                analysis_stats = {"attempts": TOTAL_ANALYSIS_ATTEMPTS, "errors": TOTAL_ANALYSIS_ERRORS, "last_success": LAST_SUCCESS_TIME}
                final_signal_data = None
                if neutral_candidates:
                    best_neutral = max(neutral_candidates, key=lambda c: c['confidence'])
                    final_signal_data = {**best_neutral, 'analysis_stats': analysis_stats}
                else:
                    final_signal_data = {
                        "side": "Neutral", "symbol": "FALLBACK", "confidence": 0.0,
                        "regime": "データ不足/レンジ", "is_fallback": True,
                        "macro_context": macro_context_data, "wave_phase": "N/A", "depth_ratio": 0.5,
                        "analysis_stats": analysis_stats, "sentiment_score": 0.5
                    }
                neutral_msg = format_telegram_message(final_signal_data)
                NEUTRAL_NOTIFIED_TIME = current_time
                await loop.run_in_executor(None, lambda: send_telegram_html(neutral_msg, is_emergency=False))
                logging.info("➡️ 強制/中立通知を実行しました。")


            await asyncio.sleep(LOOP_INTERVAL)

        except asyncio.CancelledError:
            logging.warning("バックグラウンドタスクがキャンセルされました。")
            break
        except Exception as e:
            logging.error(f"メインループで予期せぬエラーが発生しました: {type(e).__name__}: {e}。{LOOP_INTERVAL}秒後に再試行します。")
            await asyncio.sleep(LOOP_INTERVAL)


def format_telegram_message(signal: Dict) -> str:
    is_fallback = signal.get('is_fallback', False)
    vix_level = signal['macro_context']['vix_level']
    vix_status = f"VIX: {vix_level:.1f}" if vix_level > 0 else "VIX: N/A"
    gvix_level = signal['macro_context']['gvix_level']
    gvix_status = f"GVIX: {gvix_level:.1f}"

    stats = signal.get('analysis_stats', {"attempts": 0, "errors": 0, "last_success": 0})
    last_success_time = datetime.fromtimestamp(stats['last_success'], JST).strftime('%H:%M:%S') if stats['last_success'] > 0 else "N/A"

    def format_price(price):
        if signal['symbol'] in ["BTC", "ETH"]: return f"{price:,.2f}"
        return f"{price:,.4f}"

    if signal['side'] == "Neutral":
        if signal.get('is_fallback', False) and signal['symbol'] == "FALLBACK":
            error_rate = (stats['errors'] / stats['attempts']) * 100 if stats['attempts'] > 0 else 0
            return (
                f"🚨 <b>Apex BOT v8.9 - 死活監視 (システム正常)</b> 🟢\n"
                f"<i>強制通知時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i>\n\n"
                f"• **市場コンテクスト**: {signal['macro_context']['trend']} ({vix_status} | {gvix_status})\n"
                f"• **🤖 BOTヘルス**: 最終成功: {last_success_time} JST (エラー率: {error_rate:.1f}%)\n"
                f"• **データソース**: {CCXT_CLIENT_NAME} が現在メイン (エラー時即時切替)。"
            )

        tech_data = signal.get('tech_data', {})
        rsi_str = f"RSI: {tech_data.get('rsi', 50):.1f}"
        macd_hist_str = f"MACD Hist: {tech_data.get('macd_hist', 0):.4f}"
        adx_str = f"ADX: {tech_data.get('adx', 25):.1f}"
        cci_str = f"CCI: {tech_data.get('cci_signal', 0):.1f}"
        sentiment_pct = signal.get('sentiment_score', 0.5) * 100

        source = "YFinance (簡易分析)" if is_fallback else CCXT_CLIENT_NAME
        depth_ratio = signal.get('depth_ratio', 0.5)
        depth_status = "買い圧優勢" if depth_ratio > 0.52 else ("売り圧優勢" if depth_ratio < 0.48 else "均衡")
        confidence_pct = signal['confidence'] * 200

        return (
            f"⚠️ <b>市場分析速報: {signal['regime']} (中立)</b> ⏸️\n"
            f"**信頼度**: {confidence_pct:.1f}% 📉\n"
            f"---------------------------\n"
            f"• <b>ソース/波形</b>: {source} | {signal['wave_phase']}\n"
            f"• <b>需給バランス</b>: {depth_status} (比率: {depth_ratio:.2f})\n"
            f"• <b>チャート動向</b>: {rsi_str}, MACD: {macd_hist_str}\n"
            f"• <b>トレンド強度/過熱</b>: {adx_str} / {cci_str}\n" 
            f"• <b>ニュース感情</b>: {sentiment_pct:.1f}% Positive\n"
            f"<b>【BOTの判断】: 現在は待機が最適です。</b>"
        )

    score = signal['score']
    side_icon = "⬆️ LONG" if signal['side'] == "ロング" else "⬇️ SHORT"
    source = signal.get('source', 'N/A')

    if score >= 0.80: score_icon = "🔥🔥🔥"; lot_size = "MAX"; action = "積極的なエントリー"
    elif score >= 0.65: score_icon = "🔥🌟"; lot_size = "中〜大"; action = "標準的なエントリー"
    elif score >= 0.50: score_icon = "✨"; lot_size = "小"; action = "少額で慎重にエントリー"
    else: score_icon = "❌"; lot_size = "見送り"; action = "分析エラーまたは品質不足"

    tech_data = signal.get('tech_data', {})
    rsi_str = f"{tech_data.get('rsi', 50):.1f}"
    macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"
    adx_str = f"{tech_data.get('adx', 25):.1f}"
    cci_str = f"{tech_data.get('cci_signal', 0):.1f}"
    sentiment_pct = signal.get('sentiment_score', 0.5) * 100

    return (
        f"{score_icon} **{signal['symbol']} - {side_icon} シグナル発生!** {score_icon}\n"
        f"<b>信頼度スコア: {score * 100:.2f}%</b>\n"
        f"-----------------------------------------\n"
        f"• <b>現在価格</b>: ${format_price(signal['price'])}\n"
        f"\n"
        f"🎯 <b>エントリー</b>: **${format_price(signal['entry'])}**\n"
        f"🟢 <b>利確 (TP1)</b>: **${format_price(signal['tp1'])}**\n"
        f"🔴 <b>損切 (SL)</b>: **${format_price(signal['sl'])}**\n"
        f"\n"
        f"📈 <b>複合分析</b>:\n"
        f"  - <i>RSI / MACD Hist</i>: {rsi_str} / {macd_hist_str}\n"
        f"  - <i>ADX (強度) / CCI (過熱)</i>: {adx_str} / {cci_str}\n" 
        f"• <i>ニュース感情</i>: {sentiment_pct:.1f}% Positive | <i>波形</i>: {signal['wave_phase']}\n"
        f"• <i>マクロ環境</i>: {vix_status} | {gvix_status} (ソース: {source})\n"
        f"\n"
        f"💰 <b>取引示唆</b>:\n"
        f"  - <b>推奨ロット</b>: {lot_size}\n"
        f"  - <b>推奨アクション</b>: {action}\n"
        f"<b>【BOTの判断】: 取引計画に基づきエントリーを検討してください。</b>"
    )


# ------------------------------------------------------------------------------------
# FASTAPI WEB SERVER SETUP
# ------------------------------------------------------------------------------------

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    logging.info("Starting Apex BOT Web Service (v8.9 - Final Stability Release)...")
    initialize_ccxt_client()

    port = int(os.environ.get("PORT", 8000))
    logging.info(f"Web service attempting to bind to port: {port}")

    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    for client in CCXT_CLIENTS_DICT.values():
        if client:
            await client.close()
    logging.info("CCXT Clients closed during shutdown.")

@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False) 
async def read_root(request: Request):
    """
    Renderのヘルスチェックと自己Pingに応答するためのルート。
    """
    monitor_info = ", ".join(CURRENT_MONITOR_SYMBOLS[:5]) + f"...({len(CURRENT_MONITOR_SYMBOLS)} total)"
    last_health_time = ACTIVE_CLIENT_HEALTH.get(CCXT_CLIENT_NAME, 0)
    last_health_str = datetime.fromtimestamp(last_health_time).strftime('%H:%M:%S') if last_health_time > 0 else "N/A"
    
    response_data = {
        "status": "Running",
        "service": "Apex BOT v8.9 (Final Stability Release)",
        "monitoring_base": CCXT_CLIENT_NAME,
        "client_health": f"Last Success: {last_health_str}",
        "monitored_symbols": monitor_info,
        "analysis_interval_s": LOOP_INTERVAL,
        "last_analysis_attempt": datetime.fromtimestamp(LAST_UPDATE_TIME).strftime('%H:%M:%S') if LAST_UPDATE_TIME > 0 else "N/A",
    }
    
    # HEADリクエストの場合はコンテンツを返さず、ヘッダーのみ返す (RenderのLiveness Check対策)
    if request.method == "HEAD":
        # Content-Typeが設定されるようにJSONResponseを使用
        return JSONResponse(content={}, headers={"Content-Length": "0"})
    
    return response_data
