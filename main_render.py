# ====================================================================================
# Apex BOT v9.2.0-Apex - TOP30分散監視対応版 (FULL)
# 修正点: 
# 1. 銘柄リストをTOP30に拡張 (DEFAULT_SYMBOLSは廃止し、動的に取得)。
# 2. update_monitor_symbols_dynamicallyで出来高上位銘柄を取得するロジックを実装。
# 3. メインループで、TOP30銘柄を3つのクライアント（OKX, Kraken, Coinbase）に均等に分散。
# 4. SYMBOL_WAIT (銘柄ごとの遅延) を導入し、APIコール頻度を下げてレート制限を回避。
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import ccxt 
import numpy as np
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any, Callable
import yfinance as yf
import asyncio
import random
from fastapi import FastAPI
from fastapi.responses import JSONResponse 
import uvicorn
from dotenv import load_dotenv
import sys 

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP30を監視するため、DEFAULT_SYMBOLSは起動時のフォールバックとしてのみ使用
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"] 
TOP_SYMBOL_LIMIT = 30 # 出来高TOP30
SYMBOL_WAIT = 0.5 # 🚨 銘柄ごとの分析間に挿入する遅延（0.5秒）

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# 設定値 (安定性向上のための設定は維持)
LOOP_INTERVAL = 90         
PING_INTERVAL = 8          
DYNAMIC_UPDATE_INTERVAL = 60 * 30 # 銘柄リストの更新頻度を30分に設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2
BEST_POSITION_INTERVAL = 60 * 60 * 12
SIGNAL_THRESHOLD = 0.55 
CLIENT_COOLDOWN = 60 * 15  
REQUIRED_OHLCV_LIMITS = {'15m': 150, '1h': 150, '4h': 150} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

# グローバル状態変数
CCXT_CLIENTS_DICT: Dict[str, ccxt_async.Exchange] = {}
CCXT_CLIENT_NAMES: List[str] = []
CCXT_CLIENT_NAME: str = 'Initializing' 
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
NEUTRAL_NOTIFIED_TIME: float = 0
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_BEST_POSITION_TIME: float = 0 
LAST_SUCCESS_TIME: float = 0.0
TOTAL_ANALYSIS_ATTEMPTS: int = 0
TOTAL_ANALYSIS_ERRORS: int = 0
ACTIVE_CLIENT_HEALTH: Dict[str, float] = {} 
BTC_DOMINANCE_CONTEXT: Dict = {} 

# ロギング設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    stream=sys.stdout, 
                    force=True)
logging.getLogger('ccxt').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)


# ====================================================================================
# UTILITIES & CLIENTS (CCXT実装)
# ====================================================================================

# ... (format_price_utility, send_telegram_html, send_test_message, get_crypto_macro_context, get_news_sentiment は変更なし)

def format_price_utility(price: float, symbol: str) -> str:
    """価格表示をシンボルに応じて整形"""
    base_sym = symbol.split('/')[0]
    if base_sym in ["BTC", "ETH"]:
        return f"{price:,.2f}"
    elif price > 1.0:
        return f"{price:,.4f}"
    else:
        return f"{price:.6f}"

def initialize_ccxt_client():
    """CCXTクライアントを初期化（非同期）"""
    global CCXT_CLIENTS_DICT, CCXT_CLIENT_NAMES, ACTIVE_CLIENT_HEALTH
    # タイムアウト設定は維持
    clients = {
        'OKX': ccxt_async.okx({"enableRateLimit": True, "timeout": 45000}),     
        'Coinbase': ccxt_async.coinbase({"enableRateLimit": True, "timeout": 30000,
                                         "options": {"defaultType": "spot", "fetchTicker": "public"}}),
        'Kraken': ccxt_async.kraken({"enableRateLimit": True, "timeout": 30000}), 
    }
    CCXT_CLIENTS_DICT = clients
    CCXT_CLIENT_NAMES = list(CCXT_CLIENTS_DICT.keys())
    ACTIVE_CLIENT_HEALTH = {name: time.time() for name in CCXT_CLIENT_NAMES}
    logging.info(f"✅ CCXTクライアント初期化完了。利用可能なクライアント: {CCXT_CLIENT_NAMES}")

def send_telegram_html(text: str, is_emergency: bool = False):
    """HTML形式でTelegramにメッセージを送信する（ブロッキング）"""
    if 'YOUR' in TELEGRAM_TOKEN:
        clean_text = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("<code>", "").replace("</code>", "").replace("\n", " ").replace("•", "").replace("-", "").strip()
        logging.warning("⚠️ TELEGRAM_TOKENが初期値です。ログに出力されます。")
        logging.info("--- TELEGRAM通知（ダミー）---\n" + clean_text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True, "disable_notification": not is_emergency
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram送信エラーが発生しました: {e}")

async def send_test_message():
    """起動テスト通知"""
    test_text = (
        f"🤖 <b>Apex BOT v9.2.0-Apex - 起動テスト通知 (TOP30分散対応版)</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"<b>機能強化: 出来高TOP30を3つのクライアントに分散して監視します。</b>"
    )
    try:
        await asyncio.to_thread(lambda: send_telegram_html(test_text, is_emergency=True)) 
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[List[float]], str, str]:
    """CCXTからOHLCVデータを取得し、レート制限エラーを捕捉"""
    client = CCXT_CLIENTS_DICT.get(client_name)
    if not client: return [], "ClientError", client_name
    
    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 150)
    try:
        ohlcv = await client.fetch_ohlcv(symbol, timeframe, limit=limit)
        if len(ohlcv) < limit:
            return ohlcv, "DataShortage", client_name
        return ohlcv, "Success", client_name
        
    except ccxt.RateLimitExceeded:
        return [], "RateLimit", client_name
    except ccxt.ExchangeError as e:
        return [], "ExchangeError", client_name
    except ccxt.NetworkError:
        return [], "Timeout", client_name
    except Exception as e:
        return [], "UnknownError", client_name

async def fetch_order_book_depth_async(client_name: str, symbol: str) -> Dict:
    """CCXTからオーダーブック深度を取得し、買い/売り圧を計算"""
    client = CCXT_CLIENTS_DICT.get(client_name)
    if not client: return {"depth_ratio": 0.5, "total_depth": 0.0}

    try:
        orderbook = await client.fetch_order_book(symbol, limit=20) 
        
        total_bid_amount = sum(amount * price for price, amount in orderbook.get('bids', []))
        total_ask_amount = sum(amount * price for price, amount in orderbook.get('asks', []))
        
        total_depth = total_bid_amount + total_ask_amount
        depth_ratio = total_bid_amount / total_depth if total_depth > 0 else 0.5
        
        return {"depth_ratio": depth_ratio, "total_depth": total_depth}

    except Exception as e:
        return {"depth_ratio": 0.5, "total_depth": 0.0}

def get_crypto_macro_context() -> Dict:
    """仮想通貨のマクロ環境を取得 (BTC Dominance)"""
    context = {"trend": "中立", "btc_dominance": 0.0, "dominance_change_boost": 0.0}
    try:
        btc_d = yf.Ticker("BTC-USD").history(period="7d", interval="1d")
        if not btc_d.empty and len(btc_d) >= 7:
            latest_price = btc_d['Close'].iloc[-1]
            oldest_price = btc_d['Close'].iloc[0]
            dominance_7d_change = (latest_price / oldest_price - 1) * 100
            
            context["btc_dominance"] = latest_price
            
            if dominance_7d_change > 5:
                context["trend"] = "BTC優勢 (リスクオフ傾向)"
                context["dominance_change_boost"] = -0.1
            elif dominance_7d_change < -5:
                context["trend"] = "アルト優勢 (リスクオン傾向)"
                context["dominance_change_boost"] = 0.1
            
    except Exception:
        pass
    return context

def get_news_sentiment(symbol: str) -> Dict:
    """ニュース感情スコア（簡易版）"""
    sentiment_score = 0.5 + random.uniform(-0.1, 0.1) 
    return {"sentiment_score": np.clip(sentiment_score, 0.0, 1.0)}

# ... (format_telegram_message, format_best_position_message は変更なし)

# ... (calculate_trade_levels, calculate_technical_indicators, get_timeframe_trend, get_mtfa_score_adjustment, market_analysis_and_score, generate_signal_candidate は変更なし)


# ====================================================================================
# CORE ANALYSIS FUNCTIONS (変更なし)
# ====================================================================================

# ... (calculate_trade_levels)
# ... (calculate_technical_indicators)
# ... (get_timeframe_trend)
# ... (get_mtfa_score_adjustment)
# ... (market_analysis_and_score)
# ... (generate_signal_candidate)

def calculate_trade_levels(price: float, side: str, atr_value: float, score: float) -> Dict:
    if atr_value <= 0: return {"entry": price, "sl": price, "tp1": price, "tp2": price}
    rr_multiplier = np.clip(2.0 + (score - 0.55) * 5.0, 2.0, 4.0)
    sl_dist = 1.0 * atr_value
    tp1_dist = rr_multiplier * atr_value
    entry = price
    if side == "ロング":
        sl = entry - sl_dist
        tp1 = entry + tp1_dist
    else:
        sl = entry + sl_dist
        tp1 = entry - tp1_dist
    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": entry}

def calculate_technical_indicators(ohlcv: List[List[float]]) -> Dict:
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
    if len(df) < 50:
          return {"rsi": 50, "macd_hist": 0, "adx": 25, "atr_value": 0, "bb_width_pct": 0, "ma_position_score": 0, "ma_position": "中立", "df": df}
    df.ta.macd(append=True)
    df.ta.rsi(append=True)
    df.ta.adx(append=True)
    df.ta.atr(append=True)
    bbands = df.ta.bbands()
    df.ta.sma(length=20, append=True)
    df.ta.sma(length=50, append=True)
    last = df.iloc[-1]
    bb_width_col = bbands.columns[bbands.columns.str.contains('BBW_')].tolist()
    bb_width = last[bb_width_col[0]] if bb_width_col and not pd.isna(last[bb_width_col[0]]) else 0.0
    bb_width_pct = bb_width / last['SMA_20'] * 100 if last['SMA_20'] > 0 and not pd.isna(last['SMA_20']) else 0
    ma_pos_score = 0
    ma_position = "中立"
    if last['Close'] > last['SMA_20'] and last['SMA_20'] > last['SMA_50']:
        ma_pos_score = 0.3
        ma_position = "強力なロングトレンド"
    elif last['Close'] < last['SMA_20'] and last['SMA_20'] < last['SMA_50']:
        ma_pos_score = -0.3
        ma_position = "強力なショートトレンド"
    macd_hist_col = df.columns[df.columns.str.startswith('MACDH_')].tolist()
    adx_col = df.columns[df.columns.str.startswith('ADX_')].tolist()
    atr_col = df.columns[df.columns.str.startswith('ATR_')].tolist()
    rsi_col = df.columns[df.columns.str.startswith('RSI_')].tolist()
    return {
        "rsi": last[rsi_col[0]] if rsi_col and not pd.isna(last[rsi_col[0]]) else 50,
        "macd_hist": last[macd_hist_col[0]] if macd_hist_col and not pd.isna(last[macd_hist_col[0]]) else 0,
        "adx": last[adx_col[0]] if adx_col and not pd.isna(last[adx_col[0]]) else 25,
        "atr_value": last[atr_col[0]] if atr_col and not pd.isna(last[atr_col[0]]) else 0,
        "bb_width_pct": bb_width_pct,
        "ma_position_score": ma_pos_score,
        "ma_position": ma_position,
        "df": df
    }

def get_timeframe_trend(tech_data: Dict) -> str:
    ma_score = tech_data.get('ma_position_score', 0)
    adx = tech_data.get('adx', 25)
    if adx < 20: 
        return "Neutral"
    if ma_score > 0.1 and adx > 25:
        return "ロング"
    elif ma_score < -0.1 and adx > 25:
        return "ショート"
    return "Neutral"

def get_mtfa_score_adjustment(side: str, h1_trend: str, h4_trend: str, rsi_15m: float, rsi_h1: float) -> Tuple[float, Dict]:
    adjustment = 0.0
    mtfa_data = {'h1_trend': h1_trend, 'h4_trend': h4_trend}
    if side != "Neutral":
        if h4_trend == side: adjustment += 0.10
        elif h4_trend != "Neutral": adjustment -= 0.10 
        if h1_trend == side: adjustment += 0.05
        elif h1_trend != "Neutral" and h1_trend == h4_trend: adjustment -= 0.05
    if side == "ロング":
        if rsi_15m > 70 and rsi_h1 > 60: adjustment -= 0.10
    elif side == "ショート":
        if rsi_15m < 30 and rsi_h1 < 40: adjustment -= 0.10
    return adjustment, mtfa_data

def market_analysis_and_score(symbol: str, tech_data_15m: Dict, tech_data_h1: Dict, tech_data_h4: Dict, depth_data: Dict, sentiment_data: Dict, macro_context: Dict) -> Tuple[float, str, str, Dict, bool]:
    df_15m = tech_data_15m.get('df')
    if df_15m is None or len(df_15m) < 50: return 0.5, "Neutral", "不明", {}, False
    adx_15m = tech_data_15m.get('adx', 25)
    bb_width_pct_15m = tech_data_15m.get('bb_width_pct', 0)
    if bb_width_pct_15m < 2.0 and adx_15m < 25:
        regime = "レンジ相場 (抑制)"; regime_boost = 0.0
    elif bb_width_pct_15m > 4.0 and adx_15m > 30:
        regime = "トレンド相場 (強化)"; regime_boost = 0.1
    else:
        regime = "移行期"; regime_boost = 0.05
    rsi_15m = tech_data_15m.get('rsi', 50)
    macd_hist_15m = tech_data_15m.get('macd_hist', 0)
    ma_pos_score_15m = tech_data_15m.get('ma_position_score', 0)
    adx_direction_score = ma_pos_score_15m * (np.clip((adx_15m - 20) / 20, 0, 1) * 0.5 + 0.5)
    momentum_bias = ((rsi_15m - 50) / 50 * 0.15) * 0.4 + (np.clip(macd_hist_15m * 10, -0.15, 0.15)) * 0.6
    trend_bias = ma_pos_score_15m * 0.5 + adx_direction_score * 0.5
    composite_momentum_boost = 0.0
    if macd_hist_15m > 0 and rsi_15m > 55: composite_momentum_boost = 0.05
    elif macd_hist_15m < 0 and rsi_15m < 45: composite_momentum_boost = -0.05
    depth_ratio = depth_data.get('depth_ratio', 0.5)
    sentiment_score = sentiment_data.get('sentiment_score', 0.5)
    depth_bias = (depth_ratio - 0.5) * 0.2
    sentiment_bias = (sentiment_score - 0.5) * 0.1
    base_score = 0.5
    weighted_bias = (momentum_bias * 0.3) + (trend_bias * 0.3) + (depth_bias * 0.1) + (sentiment_bias * 0.1) + (composite_momentum_boost * 0.2)
    tentative_score = np.clip(base_score + weighted_bias + regime_boost * np.sign(weighted_bias), 0.0, 1.0)
    if tentative_score > 0.5: side = "ロング"
    elif tentative_score < 0.5: side = "ショート"
    else: side = "Neutral"
    h1_trend = get_timeframe_trend(tech_data_h1)
    h4_trend = get_timeframe_trend(tech_data_h4)
    rsi_h1 = tech_data_h1.get('rsi', 50)
    mtfa_adjustment, mtfa_data = get_mtfa_score_adjustment(side, h1_trend, h4_trend, rsi_15m, rsi_h1)
    macro_adjustment = macro_context.get('dominance_change_boost', 0.0) * (0.5 if symbol != 'BTC/USDT' else 0.0)
    volatility_penalty = 0.0
    volatility_penalty_applied = False
    if bb_width_pct_15m > VOLATILITY_BB_PENALTY_THRESHOLD and adx_15m < 40:
        volatility_penalty = -0.1; volatility_penalty_applied = True
    final_score = np.clip(tentative_score + mtfa_adjustment + macro_adjustment + volatility_penalty, 0.0, 1.0)
    if final_score > 0.5 + SIGNAL_THRESHOLD / 2:
        final_side = "ロング"
    elif final_score < 0.5 - SIGNAL_THRESHOLD / 2:
        final_side = "ショート"
    else:
        final_side = "Neutral"
    display_score = abs(final_score - 0.5) * 2 if final_side != "Neutral" else abs(final_score - 0.5)
    return display_score, final_side, regime, mtfa_data, volatility_penalty_applied

async def generate_signal_candidate(symbol: str, macro_context_data: Dict, client_name: str) -> Optional[Dict]:
    sentiment_data = get_news_sentiment(symbol)
    tasks = {
        '15m': fetch_ohlcv_with_fallback(client_name, symbol, '15m'),
        '1h': fetch_ohlcv_with_fallback(client_name, symbol, '1h'),
        '4h': fetch_ohlcv_with_fallback(client_name, symbol, '4h'),
        'depth': fetch_order_book_depth_async(client_name, symbol)
    }
    results = await asyncio.gather(*tasks.values())
    ohlcv_data = {'15m': results[0][0], '1h': results[1][0], '4h': results[2][0]}
    status_data = {'15m': results[0][1], '1h': results[1][1], '4h': results[2][1]} 
    depth_data = results[3]
    if status_data['15m'] in ["RateLimit", "Timeout", "ExchangeError", "UnknownError"] or not ohlcv_data['15m']:
        return {"symbol": symbol, "side": status_data['15m'], "score": 0.0, "client": client_name} 
    tech_data_15m_full = calculate_technical_indicators(ohlcv_data['15m'])
    tech_data_h1_full = calculate_technical_indicators(ohlcv_data['1h'])
    tech_data_h4_full = calculate_technical_indicators(ohlcv_data['4h'])
    tech_data_15m = {k: v for k, v in tech_data_15m_full.items() if k != 'df'}
    final_score, final_side, regime, mtfa_data, volatility_penalty_applied = market_analysis_and_score(
        symbol, tech_data_15m_full, tech_data_h1_full, tech_data_h4_full, 
        depth_data, sentiment_data, macro_context_data
    )
    current_price = tech_data_15m_full['df']['Close'].iloc[-1]
    atr_value = tech_data_15m.get('atr_value', 0)
    trade_levels = calculate_trade_levels(current_price, final_side, atr_value, final_score)
    if final_side == "Neutral":
        return {"symbol": symbol, "side": "Neutral", "confidence": final_score, "regime": regime,
                "macro_context": macro_context_data, "is_fallback": status_data['15m'] != "Success",
                "depth_ratio": depth_data['depth_ratio'], "total_depth": depth_data['total_depth'],
                "tech_data": tech_data_15m}
    source = client_name
    return {"symbol": symbol, "side": final_side, "price": current_price, "score": final_score,
            "entry": trade_levels['entry'], "sl": trade_levels['sl'],
            "tp1": trade_levels['tp1'], "tp2": trade_levels['tp2'],
            "regime": regime, "is_fallback": status_data['15m'] != "Success", 
            "depth_ratio": depth_data['depth_ratio'], "total_depth": depth_data['total_depth'],
            "macro_context": macro_context_data, "source": source, 
            "sentiment_score": sentiment_data["sentiment_score"],
            "tech_data": tech_data_15m,
            "mtfa_data": mtfa_data,
            "volatility_penalty_applied": volatility_penalty_applied,
            "client": client_name} 


# -----------------------------------------------------------------------------------
# ASYNC TASKS & MAIN LOOP
# -----------------------------------------------------------------------------------

async def update_monitor_symbols_dynamically(client_name: str, limit: int) -> List[str]:
    """
    出来高上位銘柄リストをCCXTから取得。
    KrakenまたはCoinbaseを使用して取得を試みる（OKXはfetch_tickersが遅い傾向があるため）。
    """
    global CURRENT_MONITOR_SYMBOLS
    logging.info(f"🔄 銘柄リストを更新します。出来高TOP{limit}銘柄を取得試行...")
    
    # 出来高取得を試みるクライアント (OKXを避ける)
    fetch_client_names = ['Kraken', 'Coinbase']
    new_symbols = []

    for name in fetch_client_names:
        client = CCXT_CLIENTS_DICT.get(name)
        if not client: continue

        try:
            # fetch_tickersで最新価格と出来高を取得
            tickers = await client.fetch_tickers()
            
            # USDTペアかつ出来高がゼロでない銘柄をフィルタリング
            usdt_pairs = {
                symbol: ticker.get('quoteVolume', 0) 
                for symbol, ticker in tickers.items() 
                if symbol.endswith('/USDT') and ticker.get('quoteVolume', 0) > 0
            }

            # 出来高でソートし、TOP_SYMBOL_LIMIT個を取得
            sorted_pairs = sorted(usdt_pairs.items(), key=lambda item: item[1], reverse=True)
            new_symbols = [symbol for symbol, volume in sorted_pairs[:limit]]
            
            if new_symbols:
                logging.info(f"✅ クライアント {name} を使用し、出来高TOP{len(new_symbols)}銘柄を取得しました。")
                CURRENT_MONITOR_SYMBOLS = new_symbols
                return new_symbols

        except ccxt.NetworkError:
             logging.warning(f"⚠️ クライアント {name} でネットワークエラーが発生し、銘柄リストを取得できませんでした。")
        except ccxt.ExchangeError:
             logging.warning(f"⚠️ クライアント {name} でExchangeErrorが発生し、銘柄リストを取得できませんでした。")
        except Exception as e:
            logging.error(f"❌ 銘柄リスト取得中に不明なエラー: {e}")

    # 全クライアントで失敗した場合、フォールバック
    if not new_symbols:
        logging.warning(f"❌ 出来高TOP銘柄の取得に失敗しました。フォールバックとして {len(DEFAULT_SYMBOLS)}銘柄を使用します。")
        CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS
        return DEFAULT_SYMBOLS

    return CURRENT_MONITOR_SYMBOLS


async def instant_price_check_task():
    while True:
        await asyncio.sleep(15)
        
async def self_ping_task(interval: int):
    """BOTのヘルスチェックを定期的に実行するタスク"""
    global NEUTRAL_NOTIFIED_TIME
    while True:
        await asyncio.sleep(interval)
        if time.time() - NEUTRAL_NOTIFIED_TIME > 60 * 60 * 2: 
            stats = {"attempts": TOTAL_ANALYSIS_ATTEMPTS, "errors": TOTAL_ANALYSIS_ERRORS, "last_success": LAST_SUCCESS_TIME}
            health_signal = {"symbol": "BOT", "side": "Neutral", "confidence": 0.5, "regime": "N/A", "macro_context": BTC_DOMINANCE_CONTEXT, "is_health_check": True, "analysis_stats": stats, "tech_data": {}, "depth_ratio": 0.5, "total_depth": 0}
            asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(health_signal))))
            NEUTRAL_NOTIFIED_TIME = time.time()

async def signal_notification_task(signals: List[Optional[Dict]]):
    """シグナル通知の処理とクールダウン管理"""
    current_time = time.time()
    
    error_signals = ["RateLimit", "Timeout", "ExchangeError", "UnknownError"]
    valid_signals = [s for s in signals if s is not None and s.get('side') not in error_signals]
    
    for signal in valid_signals:
        symbol = signal['symbol']
        side = signal['side']
        score = signal.get('score', 0.0)
        
        if side == "Neutral" and signal.get('is_health_check', False):
            asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(signal))))
            
        elif side in ["ロング", "ショート"] and score >= SIGNAL_THRESHOLD:
            if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(signal))))

async def best_position_notification_task():
    """定期的に最良のポジション候補を通知するタスク"""
    global LAST_BEST_POSITION_TIME
    while True:
        await asyncio.sleep(1)
        current_time = time.time()
        
        if current_time - LAST_BEST_POSITION_TIME >= BEST_POSITION_INTERVAL:
            strongest_signal = None
            max_score = 0
            
            for signal in LAST_ANALYSIS_SIGNALS:
                if signal.get('side') in ["ロング", "ショート"] and signal['score'] > max_score:
                    max_score = signal['score']
                    strongest_signal = signal
            
            if strongest_signal and max_score >= 0.60:
                logging.info(f"👑 最良ポジション候補: {strongest_signal['symbol']} (Score: {max_score:.2f})")
                asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_best_position_message(strongest_signal), is_emergency=False)))
                LAST_BEST_POSITION_TIME = current_time
            
async def main_loop():
    """BOTのメイン実行ループ。分析、クライアント切り替え、通知を行う。"""
    global LAST_UPDATE_TIME, LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    global ACTIVE_CLIENT_HEALTH, CCXT_CLIENT_NAMES, LAST_ANALYSIS_SIGNALS, BTC_DOMINANCE_CONTEXT

    # 初期設定とテスト
    BTC_DOMINANCE_CONTEXT = await asyncio.to_thread(get_crypto_macro_context)
    LAST_UPDATE_TIME = time.time()
    await send_test_message()
    
    # バックグラウンドタスクの開始
    asyncio.create_task(self_ping_task(interval=PING_INTERVAL)) 
    asyncio.create_task(instant_price_check_task())
    asyncio.create_task(best_position_notification_task()) 
    
    if not CCXT_CLIENT_NAMES:
        logging.error("致命的エラー: 利用可能なCCXTクライアントがありません。ループを停止します。")
        return

    # 初回銘柄リストの取得
    await update_monitor_symbols_dynamically(CCXT_CLIENT_NAMES[0], limit=TOP_SYMBOL_LIMIT)


    while True:
        await asyncio.sleep(0.005)
        current_time = time.time()
        
        # --- 1. 動的銘柄リストの更新とマクロ環境の取得 ---
        if current_time - LAST_UPDATE_TIME > DYNAMIC_UPDATE_INTERVAL:
            await update_monitor_symbols_dynamically(CCXT_CLIENT_NAMES[0], limit=TOP_SYMBOL_LIMIT)
            BTC_DOMINANCE_CONTEXT = await asyncio.to_thread(get_crypto_macro_context)
            LAST_UPDATE_TIME = current_time

        # --- 2. クライアントと銘柄の分散割り当てロジック (レート制限回避の核心) ---
        available_clients = [name for name in CCXT_CLIENT_NAMES if current_time >= ACTIVE_CLIENT_HEALTH.get(name, 0)]
        
        if not available_clients:
            logging.warning("❌ 全てのクライアントがクールダウン中です。次のインターバルまで待機します。")
            min_cooldown_end = min(ACTIVE_CLIENT_HEALTH.values()) if ACTIVE_CLIENT_HEALTH else current_time + LOOP_INTERVAL
            sleep_time = min(max(10, min_cooldown_end - current_time), 60) 
            await asyncio.sleep(sleep_time) 
            continue

        # 銘柄リストをアクティブなクライアント数で均等に分割
        analysis_queue: List[Tuple[str, str]] = [] # (symbol, client_name)
        client_index = 0
        
        for symbol in CURRENT_MONITOR_SYMBOLS:
            client_name = available_clients[client_index % len(available_clients)]
            analysis_queue.append((symbol, client_name))
            client_index += 1
            
        logging.info(f"🔍 分析開始 (対象銘柄: {len(analysis_queue)}銘柄, 分散クライアント: {len(available_clients)}/{len(CCXT_CLIENT_NAMES)}クライアント)")
        TOTAL_ANALYSIS_ATTEMPTS += 1
        
        # --- 3. 分析の実行と銘柄間の遅延 (レート制限回避の鍵) ---
        signals: List[Optional[Dict]] = []
        has_major_error = False
        
        for symbol, client_name in analysis_queue:
            
            # クライアントがクールダウンに入っていないか再チェック
            if current_time < ACTIVE_CLIENT_HEALTH.get(client_name, 0):
                 logging.warning(f"⚠️ 銘柄 {symbol} の処理中にクライアント {client_name} がクールダウンに入ったためスキップします。")
                 continue
                 
            signal = await generate_signal_candidate(symbol, BTC_DOMINANCE_CONTEXT, client_name)
            signals.append(signal)

            # エラー処理
            if signal and signal.get('side') in ["RateLimit", "Timeout", "ExchangeError", "UnknownError"]:
                client_name_errored = signal.get('client', client_name)
                cooldown_end_time = time.time() + CLIENT_COOLDOWN
                logging.error(f"❌ {signal['side']}エラー発生: クライアント {client_name_errored} のヘルスを {datetime.fromtimestamp(cooldown_end_time, JST).strftime('%H:%M:%S')} JST にリセット (クールダウン)。")
                ACTIVE_CLIENT_HEALTH[client_name_errored] = cooldown_end_time
                
                error_msg = f"🚨 {signal['side']} エラーが発生しました。クライアント **{client_name_errored}** を {CLIENT_COOLDOWN/60:.0f} 分間クールダウンします。即座にクライアント切り替え。"
                asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(error_msg, is_emergency=False)))
                
                has_major_error = True
                TOTAL_ANALYSIS_ERRORS += 1
                break # 主要なエラーが発生したら、このサイクルの残りの処理を中断
                
            # 🚨 APIレート制限回避のための銘柄間遅延
            await asyncio.sleep(SYMBOL_WAIT) 

        
        # --- 4. 最終シグナルと待機処理 ---
        LAST_ANALYSIS_SIGNALS = [s for s in signals if s is not None and s.get('side') not in ["RateLimit", "Timeout", "ExchangeError", "UnknownError"]]
        asyncio.create_task(signal_notification_task(signals))
        
        if not has_major_error:
            LAST_SUCCESS_TIME = current_time
            logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")
            await asyncio.sleep(LOOP_INTERVAL) 
        else:
            logging.info("➡️ クライアント切り替えのため、即座に次の分析サイクルに進みます。")
            await asyncio.sleep(1) 

# -----------------------------------------------------------------------------------
# FASTAPI SETUP (バージョン番号を更新)
# -----------------------------------------------------------------------------------

app = FastAPI(title="Apex BOT API", version="v9.2.0-Apex_FULL_TOP30")

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインループを開始する"""
    initialize_ccxt_client()
    logging.info("🚀 Apex BOT v9.2.0-Apex FULL_TOP30 Startup Complete.")
    
    # メインループをバックグラウンドタスクとして実行
    asyncio.create_task(main_loop())


@app.get("/status")
def get_status():
    """ヘルスチェック用のエンドポイント"""
    status_msg = {
        "status": "ok",
        "bot_version": "v9.2.0-Apex_FULL_TOP30",
        "last_success_timestamp": LAST_SUCCESS_TIME,
        "active_clients_count": len([name for name in CCXT_CLIENT_NAMES if time.time() >= ACTIVE_CLIENT_HEALTH.get(name, 0)]),
        "monitor_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "macro_context_trend": BTC_DOMINANCE_CONTEXT.get('trend', 'N/A'),
        "total_attempts": TOTAL_ANALYSIS_ATTEMPTS,
        "total_errors": TOTAL_ANALYSIS_ERRORS,
        "client_health": {name: datetime.fromtimestamp(t, JST).strftime('%Y-%m-%d %H:%M:%S') for name, t in ACTIVE_CLIENT_HEALTH.items()}
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    """ルートエンドポイント (GET/HEAD) - 稼働確認用"""
    return JSONResponse(content={"message": "Apex BOT is running (v9.2.0-Apex_FULL_TOP30)."}, status_code=200)
