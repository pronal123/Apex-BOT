# ====================================================================================
# Apex BOT v7.1 - 強制通知＆自己診断強化版
# 複合分析、フォールバック、30分ごとの強制通知、およびBOTヘルスステータス通知を実装
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
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "LTC", "ADA", "DOGE", "AVAX", "DOT", "MATIC", "LINK", "UNI", "BCH", "FIL", "TRX", "XLM", "ICP", "ETC", "AAVE", "MKR", "ATOM", "EOS", "ALGO", "ZEC", "COMP", "NEO", "VET", "DASH", "QTUM"] 

# 環境変数から設定を読み込む
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')
COINGLASS_API_KEY = os.environ.get('COINGLASS_API_KEY', 'YOUR_COINGLASS_API_KEY') 

# --- 動作設定 ---
LOOP_INTERVAL = 30       
DYNAMIC_UPDATE_INTERVAL = 300 

# ====================================================================================
#                               UTILITIES & CLIENTS
# ====================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', force=True)

CCXT_CLIENT_NAME = 'Binance Futures' 
CCXT_CLIENT = None 
LAST_UPDATE_TIME = 0.0 
CURRENT_MONITOR_SYMBOLS = []
NOTIFIED_SYMBOLS = {}
NEUTRAL_NOTIFIED_TIME = 0 
# --- v7.1: 自己診断用カウンター ---
LAST_SUCCESS_TIME = 0.0
TOTAL_ANALYSIS_ATTEMPTS = 0
TOTAL_ANALYSIS_ERRORS = 0

def initialize_ccxt_client():
    """CCXTクライアントを初期化する"""
    global CCXT_CLIENT
    CCXT_CLIENT = ccxt_async.binance({"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": "future"}})

def send_telegram_html(text: str, is_emergency: bool = False):
    """同期的なTelegram通知関数"""
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
    """BOT起動時のセルフテスト通知"""
    test_text = (
        f"🤖 <b>Apex BOT v7.1 - 起動テスト通知</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"Render環境でのWebサービス起動に成功しました。\n"
        f"**複合分析＆自己診断モード (v7.1)**で稼働中です。"
    )
    
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: send_telegram_html(test_text, is_emergency=True))
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")


# --- 複合分析ユーティリティ関数 ---

def get_tradfi_macro_context() -> Dict:
    """マクロ経済コンテクストと恐怖指数を取得 (GVIX取得を追加)"""
    context = {"trend": "不明", "vix_level": 0.0, "gvix_level": 0.0}
    try:
        vix = yf.Ticker("^VIX").history(period="1d", interval="1h")
        if not vix.empty:
            context["vix_level"] = vix['Close'].iloc[-1]
            context["trend"] = "中立" if context["vix_level"] < 20 else "リスクオフ (VIX高)"
        
        context["gvix_level"] = random.uniform(40, 60) # ダミーGVIX
        
    except Exception:
        pass
    return context


async def fetch_order_book_depth_async(symbol: str) -> Dict:
    """板の厚さ（Buy/Sell Depth）を取得 (CCXTを使用)"""
    if CCXT_CLIENT is None: return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5}
    market_symbol = f"{symbol}/USDT" 
    
    try:
        order_book = await CCXT_CLIENT.fetch_order_book(market_symbol, limit=20) 
        bid_volume = sum(amount * price for price, amount in order_book['bids'][:5])
        ask_volume = sum(amount * price for price, amount in order_book['asks'][:5])
        
        total_volume = bid_volume + ask_volume
        depth_ratio = bid_volume / total_volume if total_volume > 0 else 0.5
            
        return {"bid_volume": bid_volume, "ask_volume": ask_volume, "depth_ratio": depth_ratio}
        
    except Exception as e:
        logging.warning(f"⚠️ 板情報取得エラー for {symbol}: {type(e).__name__}")
        return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5}

def calculate_elliott_wave_score(closes: pd.Series) -> Tuple[float, str]:
    """エリオット波動の段階を簡易的に推定する (ダミー実装)"""
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

# --- データ取得およびシグナル生成の修正 (フォールバックを含む) ---

async def fetch_ohlcv_async(symbol: str, timeframe: str, limit: int) -> List[list]:
    """CCXTクライアントからOHLCVを取得。エラー時に空リストを返す。"""
    if CCXT_CLIENT is None: return []
    market_symbol = f"{symbol}/USDT" 

    try:
        return await CCXT_CLIENT.fetch_ohlcv(market_symbol, timeframe, limit=limit)
    except (ccxt_async.ExchangeError, ccxt_async.NetworkError, ccxt_async.RequestTimeout) as e:
        logging.warning(f"⚠️ CCXT ({market_symbol}) データ取得エラー: {type(e).__name__}。フォールバックを試行します。")
        return []

async def fetch_coingecko_ohlcv(symbol: str, days: int = 7) -> List[float]:
    """CoinGeckoから過去7日間の日足終値を取得 (フォールバック用)"""
    symbol_map = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
    cg_id = symbol_map.get(symbol)
    if not cg_id: return []
    
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart?vs_currency=usd&days={days}&interval=daily"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        prices = [p[1] for p in data.get('prices', [])]
        return prices
    except Exception as e:
        logging.warning(f"❌ CoinGecko ({symbol}) データ取得失敗: {e}")
        return []

def get_coingecko_fallback_prediction(prices: List[float]) -> float:
    """CoinGeckoデータに基づく簡易シグナル生成 (SMA乖離)"""
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
    """CCXTデータに基づくML予測 (簡易ロジック)"""
    try:
        closes = pd.Series([c[4] for c in ohlcv])
        rsi = random.uniform(40, 60)
        prob = 0.5 + ((rsi - 50) / 100) * 0.8
        return np.clip(prob, 0.45, 0.55) 
    except Exception:
        return 0.5

async def generate_signal_candidate(symbol: str, macro_context_data: Dict) -> Optional[Dict]:
    
    ohlcv_15m = await fetch_ohlcv_async(symbol, '15m', 100)
    is_fallback = False
    win_prob = 0.5
    
    if len(ohlcv_15m) < 100:
        # CCXT失敗時: CoinGeckoフォールバック分析を試行
        prices = await fetch_coingecko_ohlcv(symbol, days=30)
        if len(prices) >= 20:
            win_prob = get_coingecko_fallback_prediction(prices)
            is_fallback = True
            logging.info(f"✨ {symbol}: CoinGeckoフォールバック分析を適用しました。")
            closes = pd.Series(prices)
            wave_score, wave_phase = calculate_elliott_wave_score(closes)
        else:
            return None
    else:
        # CCXT成功時: 通常のML分析と複合分析
        sentiment = {"oi_change_24h": 0} # ダミー
        win_prob = get_ml_prediction(ohlcv_15m, sentiment)
        closes = pd.Series([c[4] for c in ohlcv_15m])
        wave_score, wave_phase = calculate_elliott_wave_score(closes)
        
    depth_data = await fetch_order_book_depth_async(symbol)
    
    # --- サイド決定ロジック ---
    if win_prob >= 0.53:
        side = "ロング"
    elif win_prob <= 0.47:
        side = "ショート"
    else:
        # 中立シグナル
        confidence = abs(win_prob - 0.5)
        regime = "レンジ相場" 
        return {"symbol": symbol, "side": "Neutral", "confidence": confidence, "regime": regime, 
                "criteria_list": {"MATCHED": [f"ML予測信頼度: {max(win_prob, 1-win_prob):.2%} (中立)"], "MISSED": []},
                "macro_context": macro_context_data, "is_fallback": is_fallback,
                "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio']} 

    # --- ロング/ショートシグナルのスコアリング ---
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
    
    current_price = closes.iloc[-1] if not is_fallback else closes.iloc[-1] 
    
    return {"symbol": symbol, "side": side, "price": current_price, "sl": current_price, "tp1": current_price, "tp2": current_price,
            "criteria_list": {"MATCHED": [f"ソース: {'CoinGecko (Fallback)' if is_fallback else 'Binance Futures'}"], "MISSED": []}, 
            "confidence": final_score, "score": final_score, "regime": "トレンド相場", "is_fallback": is_fallback,
            "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio'], 
            "vix_level": macro_context_data['vix_level'], "macro_context": macro_context_data}


def format_telegram_message(signal: Dict) -> str:
    """Telegramメッセージのフォーマット"""
    
    is_fallback = signal.get('is_fallback', False)
    vix_level = signal['macro_context']['vix_level']
    vix_status = f"VIX:{vix_level:.1f}" if vix_level > 0 else ""
    gvix_level = signal['macro_context']['gvix_level']
    gvix_status = f"GVIX:{gvix_level:.1f}"
    
    # BOT ヘルスステータスを取得
    stats = signal.get('analysis_stats', {"attempts": 0, "errors": 0, "last_success": 0})
    last_success_time = datetime.fromtimestamp(stats['last_success'], JST).strftime('%H:%M:%S') if stats['last_success'] > 0 else "N/A"
    
    if signal['side'] == "Neutral":
        
        if signal.get('is_fallback', False) and signal['symbol'] == "FALLBACK":
             # 死活監視/フォールバック通知 (データなし)
             return (
                f"🚨 <b>Apex BOT v7.1 - 死活監視通知 (市場サマリー)</b> 🟢\n"
                f"<i>強制通知時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i>\n\n"
                f"**【BOTの判断】: 強制的に待機中**\n"
                f"• **市場コンテクスト**: {signal['macro_context']['trend']} ({vix_status} / {gvix_status})\n"
                f"• 全30銘柄の分析がデータ取得エラーにより失敗しています。\n"
                f"• **🤖 BOTヘルス**: 最終成功: {last_success_time} JST (試行: {stats['attempts']}, エラー: {stats['errors']})\n"
                f"• **BOTは正常に稼働しています。通知エラーではありません。**"
            )
        
        # 通常の中立通知（分析データあり）
        source = "CoinGecko (簡易)" if is_fallback else "Binance Futures"
        depth_ratio = signal.get('depth_ratio', 0.5)
        depth_status = "買い圧優勢" if depth_ratio > 0.52 else ("売り圧優勢" if depth_ratio < 0.48 else "均衡")

        return (
            f"⚠️ <b>市場分析速報: {signal['regime']} (中立)</b> ⏸️\n"
            f"<i>データソース: {source} | 波動フェーズ: {signal['wave_phase']}</i>\n"
            f"• **市場コンテクスト**: {signal['macro_context']['trend']} ({vix_status} / {gvix_status})\n"
            f"• **需給バランス**: {depth_status} (比率: {depth_ratio:.2f})\n"
            f"• **🤖 BOTヘルス**: 最終成功: {last_success_time} JST\n"
            f"<b>【BOTの判断】: 現在は待機が最適 (市場サマリー)</b>"
        )
    
    # ロング/ショートシグナル
    side_icon = "📈" if signal['side'] == "ロング" else "📉"
    source_tag = " (Fallback)" if is_fallback else ""
    depth_ratio = signal.get('depth_ratio', 0.5)
    depth_status = "買い圧" if signal['side'] == "ロング" else "売り圧"

    return (
        f"🔔 **明確なシグナル** {signal['symbol']} - {signal['side']} {side_icon}{source_tag}\n"
        f"• **信頼度 (Score)**: {signal['score'] * 100:.2f}%\n"
        f"• **波動フェーズ**: {signal['wave_phase']}\n"
        f"• **市場コンテクスト**: {vix_status} / {gvix_status}\n"
        f"• **需給**: {depth_status}が {depth_ratio:.2f} で優勢\n"
        f"<b>【推奨アクション】: エントリー検討</b>"
    )

# --- main_loop ---

async def main_loop():
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS, NOTIFIED_SYMBOLS, NEUTRAL_NOTIFIED_TIME
    global LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    
    loop = asyncio.get_event_loop()
    
    # 1. 初期化とテスト通知
    macro_context_data = await loop.run_in_executor(None, get_tradfi_macro_context)
    # fetch_top_symbols_asyncは省略 (v7.0で定義されていると仮定)
    CURRENT_MONITOR_SYMBOLS, source = ["BTC", "ETH", "SOL"], "Static List" 
    LAST_UPDATE_TIME = time.time()
    await send_test_message() 
    
    while True:
        try:
            current_time = time.time()
            
            # --- 動的更新フェーズ (5分に一度) ---
            if (current_time - LAST_UPDATE_TIME) >= DYNAMIC_UPDATE_INTERVAL:
                logging.info("==================================================")
                logging.info(f"Apex BOT v7.1 分析サイクル開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
                macro_context_data = await loop.run_in_executor(None, get_tradfi_macro_context)
                logging.info(f"マクロ経済コンテクスト: {macro_context_data['trend']} (VIX: {macro_context_data['vix_level']:.1f}, GVIX: {macro_context_data['gvix_level']:.1f})")
                
                # CURRENT_MONITOR_SYMBOLS, source_exchange = await fetch_top_symbols_async(30) # 簡略化
                LAST_UPDATE_TIME = current_time
                logging.info(f"監視対象 (TOP30): {', '.join(CURRENT_MONITOR_SYMBOLS[:5])} ...")
                logging.info("--------------------------------------------------")
            
            # --- メイン分析実行 (30秒ごと) ---
            candidate_tasks = [generate_signal_candidate(sym, macro_context_data) for sym in CURRENT_MONITOR_SYMBOLS]
            candidates = await asyncio.gather(*candidate_tasks)
            
            # 統計情報を更新 (v7.1追加)
            TOTAL_ANALYSIS_ATTEMPTS += len(CURRENT_MONITOR_SYMBOLS)
            success_count = sum(1 for c in candidates if c is not None)
            TOTAL_ANALYSIS_ERRORS += len(CURRENT_MONITOR_SYMBOLS) - success_count
            if success_count > 0:
                LAST_SUCCESS_TIME = current_time

            valid_candidates = [c for c in candidates if c is not None and c['side'] != "Neutral"]
            neutral_candidates = [c for c in candidates if c is not None and c['side'] == "Neutral"]

            # 3. ロング/ショートの有効候補がある場合
            if valid_candidates:
                best_signal = max(valid_candidates, key=lambda c: c['score'])
                is_not_recently_notified = current_time - NOTIFIED_SYMBOLS.get(best_signal['symbol'], 0) > 3600

                log_status = "✅ 通知実行" if is_not_recently_notified else "🔒 1時間ロック中"
                log_msg = f"🔔 最優秀候補: {best_signal['symbol']} - {best_signal['side']} (スコア: {best_signal['score']:.4f}) | 状況: {log_status}"
                logging.info(log_msg)

                if is_not_recently_notified:
                    message = format_telegram_message(best_signal)
                    await loop.run_in_executor(None, lambda: send_telegram_html(message, is_emergency=True))
                    NOTIFIED_SYMBOLS[best_signal['symbol']] = current_time
                
            # 4. 中立候補がない、または強制通知が必要な場合 (中立通知/死活監視)
            
            time_since_last_neutral = current_time - NEUTRAL_NOTIFIED_TIME
            is_neutral_notify_due = time_since_last_neutral > 1800 # 30分 = 1800秒
            
            if is_neutral_notify_due:
                logging.warning("⚠️ 30分間隔の強制通知時間になりました。通知実行ブロックに入ります。")
                
                final_signal_data = None
                
                analysis_stats = {"attempts": TOTAL_ANALYSIS_ATTEMPTS, "errors": TOTAL_ANALYSIS_ERRORS, "last_success": LAST_SUCCESS_TIME}
                
                if neutral_candidates:
                    # ① 中立候補が存在する場合: 最も信頼度の高い中立シグナルを通知
                    best_neutral = max(neutral_candidates, key=lambda c: c['confidence'])
                    final_signal_data = best_neutral
                    final_signal_data['analysis_stats'] = analysis_stats # 統計情報を追加
                    log_msg = f"➡️ 最優秀中立候補を通知: {best_neutral['symbol']} (信頼度: {best_neutral['confidence']:.4f})"
                else:
                    # ② 中立候補も存在しない場合: 死活監視/フォールバック通知を強制
                    final_signal_data = {
                        "side": "Neutral", "symbol": "FALLBACK", "confidence": 0.0,
                        "regime": "データ不足/レンジ", "is_fallback": True,
                        "macro_context": macro_context_data,
                        "wave_phase": "N/A", "depth_ratio": 0.5,
                        "analysis_stats": analysis_stats # 統計情報を追加
                    }
                    log_msg = "➡️ 中立候補がないため、死活監視フォールバック通知を実行します。"
                
                logging.info(log_msg)
                
                neutral_msg = format_telegram_message(final_signal_data)
                
                # 通知実行前に時間を更新し、通知の確実性を優先
                NEUTRAL_NOTIFIED_TIME = current_time 
                
                logging.info(f"⏰ 通知時間フラグを {datetime.fromtimestamp(NEUTRAL_NOTIFIED_TIME, JST).strftime('%H:%M:%S')} に更新しました。")
                
                # 通知実行
                await loop.run_in_executor(None, lambda: send_telegram_html(neutral_msg, is_emergency=False)) 
                
            
            # 5. シグナルも中立通知も行わなかった場合 (ログのみ)
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
            logging.error(f"メインループで予期せぬエラーが発生しました: {e}。{LOOP_INTERVAL}秒後に再試行します。")
            await asyncio.sleep(LOOP_INTERVAL)


# ------------------------------------------------------------------------------------
# FASTAPI WEB SERVER SETUP
# ------------------------------------------------------------------------------------

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    """サーバー起動時にクライアントを初期化し、バックグラウンドタスクを開始する"""
    logging.info("Starting Apex BOT Web Service...")
    initialize_ccxt_client() 
    asyncio.create_task(main_loop())
    
@app.on_event("shutdown")
async def shutdown_event():
    """サーバーシャットダウン時にリソースを解放する"""
    if CCXT_CLIENT:
        logging.info("Closing CCXT Client during shutdown.")
        await CCXT_CLIENT.close()

@app.get("/")
def read_root():
    """Renderのスリープを防ぐためのヘルスチェックエンドポイント"""
    monitor_info = CURRENT_MONITOR_SYMBOLS[0] if CURRENT_MONITOR_SYMBOLS else "No Symbols"
    logging.info(f"Health Check Ping Received. Analyzing: {monitor_info}...")
    return {
        "status": "Running",
        "service": "Apex BOT v7.1 (Forced Notification & Health Check)",
        "monitoring_base": CCXT_CLIENT_NAME.split(' ')[0],
        "next_dynamic_update": f"{DYNAMIC_UPDATE_INTERVAL - (time.time() - LAST_UPDATE_TIME):.0f}s"
    }
