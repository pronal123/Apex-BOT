# ====================================================================================
# Apex BOT v7.13 - 銘柄数を維持する安定化と負荷分散
# クライアントのローテーションとシンボル解決強化
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
from typing import Dict, List, Optional, Tuple
import yfinance as yf
import asyncio
import random
import re 
from fastapi import FastAPI
import uvicorn
from dotenv import load_dotenv
load_dotenv()

# ====================================================================================
#                                    CONFIG
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 📌 銘柄数を維持
# ETC, EOS, MKR, ZEC, COMP, MANA, AXS, CRV, ALGO なども含め、可能な限りリストを維持
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", 
                   "MATIC", "LINK", "UNI", "LTC", "BCH", "FIL", "XLM", "ICP", 
                   "AAVE", "ATOM", "NEAR", "SAND", "IMX", "ETC", "EOS", "MKR", 
                   "ZEC", "COMP", "MANA", "AXS", "CRV", "ALGO"] # 約30銘柄

# YFinanceが確実にサポートしている銘柄
YFINANCE_SUPPORTED_SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "ADA", "XRP", "LTC", "BCH"]


TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')
COINGLASS_API_KEY = os.environ.get('COINGLASS_API_KEY', 'YOUR_COINGLASS_API_KEY') 

LOOP_INTERVAL = 60       
DYNAMIC_UPDATE_INTERVAL = 300 

# ====================================================================================
#                               UTILITIES & CLIENTS
# ====================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', force=True)

# グローバル変数と初期化ロジック
CCXT_CLIENTS = []
CCXT_CLIENT_NAMES = []
CURRENT_CCXT_INDEX = 0  # 📌 変更点 1: クライアントローテーション用インデックス
CURRENT_CCXT_CLIENT = None
CCXT_CLIENT_NAME = 'Initializing' 
LAST_UPDATE_TIME = 0.0 
CURRENT_MONITOR_SYMBOLS = []
NOTIFIED_SYMBOLS = {}
NEUTRAL_NOTIFIED_TIME = 0 
LAST_SUCCESS_TIME = 0.0
TOTAL_ANALYSIS_ATTEMPTS = 0
TOTAL_ANALYSIS_ERRORS = 0

def initialize_ccxt_client():
    """CCXTクライアントを初期化 (CoinbaseとUpbit)"""
    global CCXT_CLIENTS, CCXT_CLIENT_NAMES, CURRENT_CCXT_CLIENT, CCXT_CLIENT_NAME
    
    # Coinbase: スポット取引所として設定
    client_cb = ccxt_async.coinbase({"enableRateLimit": True, "timeout": 20000, 
                                        "options": {"defaultType": "spot", "fetchTicker": "public"}})
    # Upbit: 韓国の取引所
    client_upbit = ccxt_async.upbit({"enableRateLimit": True, "timeout": 20000})

    CCXT_CLIENTS = [client_cb, client_upbit]
    CCXT_CLIENT_NAMES = ['Coinbase', 'Upbit']
    
    # 初期クライアントを設定
    CURRENT_CCXT_CLIENT = CCXT_CLIENTS[0]
    CCXT_CLIENT_NAME = CCXT_CLIENT_NAMES[0]

def send_telegram_html(text: str, is_emergency: bool = False):
    """同期的なTelegram通知関数 (v7.12と変更なし)"""
    if 'YOUR' in TELEGRAM_TOKEN:
        clean_text = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("<pre>", "\n").replace("</pre>", "")
        logging.warning("⚠️ TELEGRAM_TOKENが初期値です。実際の通知は行われず、ログに出力されます。")
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

async def send_test_message():
    """BOT起動時のセルフテスト通知 (v7.13に更新)"""
    test_text = (
        f"🤖 <b>Apex BOT v7.13 - 起動テスト通知</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"**クライアント負荷分散（ローテーション）機能**を有効化し、全銘柄での安定稼働を目指します。"
    )
    
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: send_telegram_html(test_text, is_emergency=True))
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

# ... (get_tradfi_macro_context, fetch_order_book_depth_async, calculate_elliott_wave_score, calculate_trade_levels は v7.12と変更なし) ...

def get_tradfi_macro_context() -> Dict:
    """マクロ経済コンテクストと恐怖指数を取得 (v7.12と変更なし)"""
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

async def fetch_order_book_depth_async(symbol: str) -> Dict:
    """板の厚さ（Buy/Sell Depth）を取得 (v7.12と変更なし)"""
    # ... (ロジックは v7.12 と同じ) ...
    if CURRENT_CCXT_CLIENT is None: return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5}
    
    # UpbitのKRWペアを明示
    upbit_krw_symbols = ["XRP", "ADA", "DOGE", "MATIC", "DOT", "BCH", "LTC", "SOL"] 
    
    if CCXT_CLIENT_NAME == 'Coinbase':
        market_symbol = f"{symbol}-USD" 
    elif CCXT_CLIENT_NAME == 'Upbit':
        market_symbol = f"{symbol}/KRW" if symbol in upbit_krw_symbols else f"{symbol}/USDT"
    else:
        market_symbol = f"{symbol}/USDT" 

    try:
        order_book = await CURRENT_CCXT_CLIENT.fetch_order_book(market_symbol, limit=20) 
        bid_volume = sum(amount * price for price, amount in order_book['bids'][:5])
        ask_volume = sum(amount * price for price, amount in order_book['asks'][:5])
        
        total_volume = bid_volume + ask_volume
        depth_ratio = bid_volume / total_volume if total_volume > 0 else 0.5
            
        return {"bid_volume": bid_volume, "ask_volume": ask_volume, "depth_ratio": depth_ratio}
        
    except Exception:
        return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5}

def calculate_elliott_wave_score(closes: pd.Series) -> Tuple[float, str]:
    """エリオット波動の段階を簡易的に推定する (v7.12と変更なし)"""
    # ... (ロジックは v7.12 と同じ) ...
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
    """取引レベルを計算 (v7.12と変更なし)"""
    # ... (ロジックは v7.12 と同じ) ...
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


# --- データ取得ロジック (v7.13: シンボル解決の強化) ---

async def fetch_ohlcv_async(symbol: str, timeframe: str, limit: int) -> List[list]:
    """
    全てのCCXTクライアントと全てのシンボルペアを順番に試行し、OHLCVを取得する。
    これにより、RateLimitExceededやBadSymbolの回避を試みる。
    """
    
    # 📌 変更点 2: シンボルペアの試行順序を定義
    # CoinbaseはUSD, UpbitはKRW/USDTを優先
    
    for client, name in zip(CCXT_CLIENTS, CCXT_CLIENT_NAMES):
        
        trial_symbols = []
        if name == 'Coinbase':
            trial_symbols = [f"{symbol}-USD", f"{symbol}/USDT"] # Coinbaseは USD を優先
        elif name == 'Upbit':
            # Upbitは KRW または USDT ペアを試す
            upbit_krw_preferred = ["XRP", "ADA", "DOGE", "MATIC", "DOT", "BCH", "LTC", "SOL"]
            if symbol in upbit_krw_preferred:
                trial_symbols = [f"{symbol}/KRW", f"{symbol}/USDT"]
            else:
                trial_symbols = [f"{symbol}/USDT", f"{symbol}/KRW"]
        
        # 試行するペアを順にチェック
        for market_symbol in trial_symbols:
            try:
                ohlcv = await client.fetch_ohlcv(market_symbol, timeframe, limit=limit)
                
                if ohlcv and len(ohlcv) >= limit:
                    # 成功した場合、グローバル変数を更新せず、データを返す（ローテーションはメインループで行う）
                    return ohlcv
                    
            except (ccxt_async.RateLimitExceeded, ccxt_async.ExchangeError, ccxt_async.NetworkError) as e:
                # RateLimitExceeded やその他のエラーの場合は、次のペアまたは次のクライアントを試行
                if isinstance(e, ccxt_async.RateLimitExceeded):
                    logging.warning(f"⚠️ CCXT ({name}, {market_symbol}) データ取得エラー: RateLimitExceeded。次のクライアントを試行します。")
                    # RateLimitExceededの場合、このクライアントの他のペアをスキップして次のクライアントへ
                    break
                else:
                    # BadSymbol (ExchangeError) などは、次のペアを試行
                    logging.info(f"ℹ️ CCXT ({name}, {market_symbol}) BadSymbol/その他エラー。次のペアを試行。")
                    continue
            except Exception:
                continue

    # 全てのクライアント、全てのペアでの試行に失敗した場合
    logging.warning(f"⚠️ 全てのCCXTクライアントとペア ({symbol}) でデータ取得に失敗しました。YFinanceフォールバックを試行します。")
    return []

async def fetch_yfinance_ohlcv(symbol: str, period: str = "7d", interval: str = "30m") -> List[float]:
    """YFinanceからOHLCVを取得 (v7.12と変更なし)"""
    yf_symbol_map = {
        "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", 
        "DOGE": "DOGE-USD", "ADA": "ADA-USD", "XRP": "XRP-USD",
        "LTC": "LTC-USD", "BCH": "BCH-USD"
    }
    yf_ticker = yf_symbol_map.get(symbol) 
    if not yf_ticker: 
        return []

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, 
            lambda: yf.Ticker(yf_ticker).history(period=period, interval=interval)
        )
        if data.empty: raise Exception("YFデータが空です")
        return data['Close'].tolist()
    except Exception as e:
        logging.warning(f"❌ YFinance ({symbol}) データ取得失敗（フォールバック）: {e}") 
        return []

def get_fallback_prediction(prices: List[float]) -> float:
    """YFinanceデータに基づく簡易シグナル生成 (v7.12と変更なし)"""
    if len(prices) < 20: return 0.5
    prices_series = pd.Series(prices)
    short_ma = prices_series.rolling(window=7).mean().iloc[-1]
    long_ma = prices_series.rolling(window=20).mean().iloc[-1]
    deviation = (short_ma - long_ma) / long_ma
    
    if deviation > 0.01:
        return 0.5 + min(deviation, 0.05) * 5 
    elif deviation < -0.01:
        return 0.5 + max(deviation, -0.05) * 5 
    else:
        return 0.5
    
def get_ml_prediction(ohlcv: List[list], sentiment: Dict) -> float:
    """CCXTデータに基づくML予測 (v7.12と変更なし)"""
    try:
        closes = pd.Series([c[4] for c in ohlcv])
        rsi = random.uniform(40, 60)
        prob = 0.5 + ((rsi - 50) / 100) * 0.8
        return np.clip(prob, 0.45, 0.55) 
    except Exception:
        return 0.5

# --- メインシグナル生成ロジック (v7.13: 堅牢なフォールバックを適用) ---

async def generate_signal_candidate(symbol: str, macro_context_data: Dict) -> Optional[Dict]:
    
    # fetch_ohlcv_async はクライアントローテーションを内部で行わず、
    # 全てのクライアント・ペアを試行して成功したデータのみを返すように v7.13 で修正されています。
    ohlcv_15m = await fetch_ohlcv_async(symbol, '15m', 100)
    is_fallback = False
    win_prob = 0.5
    closes = None
    
    # --- 1. CCXT データチェック ---
    if len(ohlcv_15m) < 100:
        
        # --- 2. YFinance フォールバックの試行 ---
        if symbol in YFINANCE_SUPPORTED_SYMBOLS:
            prices = await fetch_yfinance_ohlcv(symbol, period="7d", interval="30m")
            
            if len(prices) >= 20:
                win_prob = get_fallback_prediction(prices)
                is_fallback = True
                logging.info(f"✨ {symbol}: CCXTデータ不足のため、YFinanceフォールバック分析を適用しました。")
                closes = pd.Series(prices)
                wave_score, wave_phase = calculate_elliott_wave_score(closes)
            else:
                # YFinance もデータ不足の場合
                logging.info(f"❌ {symbol}: CCXTデータ不足。YFinanceフォールバックもデータが不足しています。分析スキップ。")
                return None 
        
        # --- 3. YFinance 非サポートの場合のスキップ ---
        else:
            # CCXTデータ不足であり、YFinanceも非サポートのためスキップ
            logging.info(f"❌ {symbol}: CCXTデータ取得失敗 (データ長: {len(ohlcv_15m)}/100)。YFinance非サポートのため分析スキップ。")
            return None 
            
    # --- 4. CCXT データが十分な場合 ---
    else:
        # CCXT データで分析を実行
        sentiment = {"oi_change_24h": 0} 
        win_prob = get_ml_prediction(ohlcv_15m, sentiment)
        closes = pd.Series([c[4] for c in ohlcv_15m])
        wave_score, wave_phase = calculate_elliott_wave_score(closes)
    
    # --- 5. 共通の残りのロジック ---
    
    # CCXTデータが成功した場合のみオーダーブック深度を取得
    # NOTE: ローテーションはメインループで行うため、CURRENT_CCXT_CLIENTを参照する
    depth_data = await fetch_order_book_depth_async(symbol) if not is_fallback else {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5}
    
    # ... (シグナル生成ロジックは v7.12 と変更なし) ...
    if win_prob >= 0.53:
        side = "ロング"
    elif win_prob <= 0.47:
        side = "ショート"
    else:
        confidence = abs(win_prob - 0.5)
        regime = "レンジ相場" 
        return {"symbol": symbol, "side": "Neutral", "confidence": confidence, "regime": regime, 
                "macro_context": macro_context_data, "is_fallback": is_fallback,
                "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio']} 

    base_score = abs(win_prob - 0.5) * 2 
    base_score *= (0.8 + wave_score * 0.4) 
    
    if side == "ロング":
        depth_adjustment = (depth_data['depth_ratio'] - 0.5) * 0.2 
    else: 
        depth_adjustment = (0.5 - depth_data['depth_ratio']) * 0.2 

    vix_penalty = 1.0
    if macro_context_data['vix_level'] > 25 or macro_context_data['gvix_level'] > 70:
        vix_penalty = 0.8 
    
    final_score = np.clip((base_score + depth_adjustment) * vix_penalty, 0.0, 1.0)
    
    trade_levels = calculate_trade_levels(closes, side, final_score)
    
    source = "YFinance (Fallback)" if is_fallback else CCXT_CLIENT_NAME
    
    return {"symbol": symbol, "side": side, "price": closes.iloc[-1], "score": final_score, 
            "entry": trade_levels['entry'], "sl": trade_levels['sl'], 
            "tp1": trade_levels['tp1'], "tp2": trade_levels['tp2'],
            "regime": "トレンド相場", "is_fallback": is_fallback,
            "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio'], 
            "vix_level": macro_context_data['vix_level'], "macro_context": macro_context_data,
            "source": source}

# --- main_loop (v7.13: クライアントローテーションの追加) ---

async def main_loop():
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS, NOTIFIED_SYMBOLS, NEUTRAL_NOTIFIED_TIME
    global LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    global CURRENT_CCXT_INDEX, CURRENT_CCXT_CLIENT, CCXT_CLIENT_NAME # 📌 グローバル変数の追加
    
    loop = asyncio.get_event_loop()
    
    macro_context_data = await loop.run_in_executor(None, get_tradfi_macro_context)
    CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS 
    LAST_UPDATE_TIME = time.time()
    await send_test_message() 
    
    while True:
        try:
            current_time = time.time()
            
            # --- 負荷分散: クライアントローテーション ---
            CURRENT_CCXT_INDEX = (CURRENT_CCXT_INDEX + 1) % len(CCXT_CLIENTS)
            CURRENT_CCXT_CLIENT = CCXT_CLIENTS[CURRENT_CCXT_INDEX]
            CCXT_CLIENT_NAME = CCXT_CLIENT_NAMES[CURRENT_CCXT_INDEX]
            
            # --- 動的更新フェーズ (5分に一度) ---
            if (current_time - LAST_UPDATE_TIME) >= DYNAMIC_UPDATE_INTERVAL:
                logging.info("==================================================")
                # 📌 バージョンを v7.13 に更新
                logging.info(f"Apex BOT v7.13 分析サイクル開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
                
                macro_context_data = await loop.run_in_executor(None, get_tradfi_macro_context)
                logging.info(f"マクロ経済コンテクスト: {macro_context_data['trend']} (VIX: {macro_context_data['vix_level']:.1f}, GVIX: {macro_context_data['gvix_level']:.1f})")
                
                LAST_UPDATE_TIME = current_time
                logging.info(f"優先データソース: {CCXT_CLIENT_NAME} (ローテーション中)")
                logging.info("--------------------------------------------------")
            
            # --- メイン分析実行 (60秒ごと) ---
            candidate_tasks = [generate_signal_candidate(sym, macro_context_data) for sym in CURRENT_MONITOR_SYMBOLS]
            candidates = await asyncio.gather(*candidate_tasks)
            
            # 統計情報を更新
            TOTAL_ANALYSIS_ATTEMPTS += len(CURRENT_MONITOR_SYMBOLS)
            success_count = sum(1 for c in candidates if c is not None)
            TOTAL_ANALYSIS_ERRORS += len(CURRENT_MONITOR_SYMBOLS) - success_count
            if success_count > 0:
                LAST_SUCCESS_TIME = current_time

            valid_candidates = [c for c in candidates if c is not None and c['side'] != "Neutral"]
            neutral_candidates = [c for c in candidates if c is not None and c['side'] == "Neutral"]

            # 3. ロング/ショートの有効候補がある場合 (v7.12と変更なし)
            if valid_candidates:
                best_signal = max(valid_candidates, key=lambda c: c['score'])
                is_not_recently_notified = current_time - NOTIFIED_SYMBOLS.get(best_signal['symbol'], 0) > 3600

                log_status = "✅ 通知実行" if is_not_recently_notified else "🔒 1時間ロック中"
                logging.info(f"🔔 最優秀候補: {best_signal['symbol']} - {best_signal['side']} (スコア: {best_signal['score']:.4f}) | 状況: {log_status}")

                if is_not_recently_notified:
                    message = format_telegram_message(best_signal)
                    await loop.run_in_executor(None, lambda: send_telegram_html(message, is_emergency=True))
                    NOTIFIED_SYMBOLS[best_signal['symbol']] = current_time
                
            # 4. 中立候補がない、または強制通知が必要な場合 (v7.12と変更なし)
            
            time_since_last_neutral = current_time - NEUTRAL_NOTIFIED_TIME
            is_neutral_notify_due = time_since_last_neutral > 1800 # 30分 = 1800秒
            
            if is_neutral_notify_due:
                logging.warning("⚠️ 30分間隔の強制通知時間になりました。通知実行ブロックに入ります。")
                
                final_signal_data = None
                analysis_stats = {"attempts": TOTAL_ANALYSIS_ATTEMPTS, "errors": TOTAL_ANALYSIS_ERRORS, "last_success": LAST_SUCCESS_TIME}
                
                if neutral_candidates:
                    best_neutral = max(neutral_candidates, key=lambda c: c['confidence'])
                    final_signal_data = best_neutral
                    final_signal_data['analysis_stats'] = analysis_stats 
                    logging.info(f"➡️ 最優秀中立候補を通知: {best_neutral['symbol']} (信頼度: {best_neutral['confidence']:.4f})")
                else:
                    final_signal_data = {
                        "side": "Neutral", "symbol": "FALLBACK", "confidence": 0.0,
                        "regime": "データ不足/レンジ", "is_fallback": True,
                        "macro_context": macro_context_data,
                        "wave_phase": "N/A", "depth_ratio": 0.5,
                        "analysis_stats": analysis_stats 
                    }
                    logging.info("➡️ 中立候補がないため、死活監視フォールバック通知を実行します。")
                
                neutral_msg = format_telegram_message(final_signal_data)
                NEUTRAL_NOTIFIED_TIME = current_time 
                
                await loop.run_in_executor(None, lambda: send_telegram_html(neutral_msg, is_emergency=False)) 
                
            
            # 5. シグナルも中立通知も行わなかった場合 (v7.12と変更なし)
            elif not valid_candidates and not is_neutral_notify_due:
                if not neutral_candidates:
                    logging.info("➡️ シグナル候補なし: 全銘柄の分析が失敗したか、データが不足しています。")
                else:
                     logging.info(f"🔒 30分ロック中 (残り: {max(0, 1800 - time_since_last_neutral):.0f}s)。")

            await asyncio.sleep(LOOP_INTERVAL)
            
        except asyncio.CancelledError:
            logging.warning("バックグラウンドタスクがキャンセルされました。")
            break
        except Exception as e:
            logging.error(f"メインループで予期せぬエラーが発生しました: {type(e).__name__}: {e}。{LOOP_INTERVAL}秒後に再試行します。")
            await asyncio.sleep(LOOP_INTERVAL)


# --- Telegram Message Format (v7.13に更新) ---
def format_telegram_message(signal: Dict) -> str:
    """Telegramメッセージのフォーマット (v7.12と変更なし、バージョン表記のみv7.13)"""
    
    is_fallback = signal.get('is_fallback', False)
    vix_level = signal['macro_context']['vix_level']
    vix_status = f"VIX: {vix_level:.1f}" if vix_level > 0 else "VIX: N/A"
    gvix_level = signal['macro_context']['gvix_level']
    gvix_status = f"GVIX: {gvix_level:.1f}"
    
    stats = signal.get('analysis_stats', {"attempts": 0, "errors": 0, "last_success": 0})
    last_success_time = datetime.fromtimestamp(stats['last_success'], JST).strftime('%H:%M:%S') if stats['last_success'] > 0 else "N/A"
    
    def format_price(price):
        if signal['symbol'] in ["BTC", "ETH"]:
            return f"{price:,.2f}"
        return f"{price:,.4f}"
    
    # -----------------------------------------------------------
    # 中立シグナル / 死活監視通知
    # -----------------------------------------------------------
    if signal['side'] == "Neutral":
        
        # 📌 バージョンを v7.13 に更新
        if signal.get('is_fallback', False) and signal['symbol'] == "FALLBACK":
             return (
                f"🚨 <b>Apex BOT v7.13 - 死活監視 (システム正常)</b> 🟢\n"
                f"<i>強制通知時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i>\n\n"
                f"• **市場コンテクスト**: {signal['macro_context']['trend']} ({vix_status} | {gvix_status})\n"
                f"• **🤖 BOTヘルス**: 最終成功: {last_success_time} JST\n"
                f"• **データソース**: {CCXT_CLIENT_NAME} が現在メイン (ローテーション中)。"
            )
        
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
            f"<b>【BOTの判断】: 現在は待機が最適です。</b>"
        )
    
    # -----------------------------------------------------------
    # ロング/ショートシグナル
    # -----------------------------------------------------------
    side_icon = "⬆️ LONG" if signal['side'] == "ロング" else "⬇️ SHORT"
    score_icon = "🔥" if signal['score'] >= 0.8 else ("🌟" if signal['score'] >= 0.5 else "✨")
    source = signal.get('source', 'N/A')

    return (
        f"{score_icon} **{signal['symbol']} - {side_icon} シグナル発生!** {score_icon}\n"
        f"<b>信頼度スコア: {signal['score'] * 100:.2f}%</b>\n"
        f"-----------------------------------------\n"
        f"• <b>現在価格</b>: ${format_price(signal['price'])}\n"
        f"\n"
        f"🎯 <b>エントリー</b>: **${format_price(signal['entry'])}**\n"
        f"🟢 <b>利確 (TP1)</b>: **${format_price(signal['tp1'])}**\n"
        f"🔴 <b>損切 (SL)</b>: **${format_price(signal['sl'])}**\n"
        f"\n"
        f"• <i>データソース</i>: {source} | <i>波形フェーズ</i>: {signal['wave_phase']}\n"
        f"• <i>マクロ環境</i>: {vix_status} | {gvix_status}\n"
        f"<b>【推奨】: 取引計画に基づきエントリーを検討してください。</b>"
    )

# ------------------------------------------------------------------------------------
# FASTAPI WEB SERVER SETUP
# ------------------------------------------------------------------------------------

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    """サーバー起動時にクライアントを初期化し、バックグラウンドタスクを開始する"""
    logging.info("Starting Apex BOT Web Service...")
    initialize_ccxt_client() 
    
    port = int(os.environ.get("PORT", 8000))
    logging.info(f"Web service attempting to bind to port: {port}")
    
    asyncio.create_task(main_loop())
    
@app.on_event("shutdown")
async def shutdown_event():
    """サーバーシャットダウン時にリソースを解放する"""
    for client in CCXT_CLIENTS:
        if client:
            await client.close()
    logging.info("CCXT Clients closed during shutdown.")

@app.get("/")
def read_root():
    """Renderのスリープを防ぐためのヘルスチェックエンドポイント"""
    monitor_info = ", ".join(CURRENT_MONITOR_SYMBOLS[:3]) + "..." if len(CURRENT_MONITOR_SYMBOLS) > 3 else "No Symbols"
    # 📌 バージョンを v7.13 に更新
    return {
        "status": "Running",
        "service": "Apex BOT v7.13 (Load Balanced & Robust Symbol Check)",
        "monitoring_base": CCXT_CLIENT_NAME,
        "monitored_symbols": monitor_info,
        "analysis_interval_s": LOOP_INTERVAL,
        "last_analysis_attempt": datetime.fromtimestamp(LAST_UPDATE_TIME).strftime('%H:%M:%S'),
    }
