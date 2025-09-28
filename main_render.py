# ====================================================================================
# Apex BOT v7.40 - 無料枠耐久 & 極限レート制限緩和バージョン
# REQUEST_DELAY 0.5s、自己pingによる稼働時間延長、ログ設定調整
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
from fastapi import FastAPI
import uvicorn
from dotenv import load_dotenv
load_dotenv() 

# ====================================================================================
#                          CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 監視対象銘柄リスト
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", 
                   "MATIC", "LINK", "UNI", "LTC", "BCH", "FIL", "XLM", "ICP", 
                   "AAVE", "ATOM", "NEAR", "SAND", "IMX", "ETC", "EOS", "MKR", 
                   "ZEC", "COMP", "MANA", "AXS", "CRV", "ALGO"] 

YFINANCE_SUPPORTED_SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "ADA", "XRP", "LTC", "BCH"]

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

LOOP_INTERVAL = 60       
DYNAMIC_UPDATE_INTERVAL = 300 
REQUEST_DELAY = 0.5     # 📌 最終調整: CCXTリクエスト間の遅延を 0.5秒に増加

# ロギング設定の強化
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname]s - %(message)s', 
                    force=True)

# 📌 修正: CCXT/urllib3のログノイズを抑制
logging.getLogger('ccxt').setLevel(logging.WARNING) 
logging.getLogger('urllib3').setLevel(logging.WARNING) 

# グローバル状態変数 (変更なし)
CCXT_CLIENTS_DICT: Dict[str, ccxt_async.Exchange] = {}
CCXT_CLIENT_NAMES: List[str] = []
CCXT_CLIENT_NAME: str = 'Initializing' 
LAST_UPDATE_TIME: float = 0.0 
CURRENT_MONITOR_SYMBOLS: List[str] = []
NOTIFIED_SYMBOLS: Dict[str, float] = {} 
NEUTRAL_NOTIFIED_TIME: float = 0 
LAST_SUCCESS_TIME: float = 0.0
TOTAL_ANALYSIS_ATTEMPTS: int = 0
TOTAL_ANALYSIS_ERRORS: int = 0

# ====================================================================================
#                             UTILITIES & CLIENTS
# ====================================================================================

def initialize_ccxt_client():
    """CCXTクライアントを初期化 - enableRateLimit: True を維持 (変更なし)"""
    global CCXT_CLIENTS_DICT, CCXT_CLIENT_NAMES, CCXT_CLIENT_NAME
    
    client_cb = ccxt_async.coinbase({"enableRateLimit": True, "timeout": 20000, 
                                        "options": {"defaultType": "spot", "fetchTicker": "public"}})
    client_upbit = ccxt_async.upbit({"enableRateLimit": True, "timeout": 20000})

    CCXT_CLIENTS_DICT = {'Coinbase': client_cb, 'Upbit': client_upbit}
    CCXT_CLIENT_NAMES = list(CCXT_CLIENTS_DICT.keys())
    CCXT_CLIENT_NAME = 'Coinbase'

def send_telegram_html(text: str, is_emergency: bool = False):
    """同期的なTelegram通知関数 (変更なし)"""
    # ... (前バージョンと同じ) ...
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

async def send_test_message():
    """起動テスト通知 (バージョン表記を v7.40 に更新)"""
    test_text = (
        f"🤖 <b>Apex BOT v7.40 - 起動テスト通知</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"**無料枠耐久**を試みます (遅延 {REQUEST_DELAY}秒, 自己Ping有効)。"
    )
    
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: send_telegram_html(test_text, is_emergency=True))
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

# ... (get_tradfi_macro_context, calculate_elliott_wave_score, calculate_trade_levels は変更なし) ...
# ... (fetch_yfinance_ohlcv, get_fallback_prediction, calculate_technical_indicators, get_ml_prediction は変更なし) ...

# --- データ取得ロジック (v7.40 - REQUEST_DELAYの増加以外は v7.30 と同じ) ---

async def fetch_ohlcv_single_client(client_name: str, symbol: str, timeframe: str, limit: int) -> Tuple[List[list], str]:
    """指定された単一のCCXTクライアントでOHLCVを取得。遅延 0.5秒を適用 (v7.40)。"""
    client = CCXT_CLIENTS_DICT.get(client_name)
    if client is None: return [], "NoClient"
    
    trial_symbols = []
    if client_name == 'Coinbase':
        trial_symbols = [f"{symbol}-USD", f"{symbol}/USDT"] 
    elif client_name == 'Upbit':
        upbit_krw_preferred = ["XRP", "ADA", "DOGE", "MATIC", "DOT", "BCH", "LTC", "SOL"] 
        if symbol in upbit_krw_preferred:
            trial_symbols = [f"{symbol}/KRW", f"{symbol}/USDT"]
        else:
            trial_symbols = [f"{symbol}/USDT", f"{symbol}/KRW"]
    
    for market_symbol in trial_symbols:
        try:
            await asyncio.sleep(REQUEST_DELAY) # 📌 遅延を適用 (0.5s)
            
            ohlcv = await client.fetch_ohlcv(market_symbol, timeframe, limit=limit)
            
            if ohlcv and len(ohlcv) >= limit:
                return ohlcv, "Success"
                
        except ccxt_async.RateLimitExceeded:
            logging.warning(f"⚠️ CCXT ({client_name}, {market_symbol}) データ取得エラー: RateLimitExceeded。処理を中断。")
            return [], "RateLimitExceeded"
            
        except ccxt_async.BadSymbol:
            logging.debug(f"ℹ️ CCXT ({client_name}, {market_symbol}) BadSymbol。次のペアを試行。")
            continue
            
        except (ccxt_async.ExchangeError, ccxt_async.NetworkError) as e:
            logging.debug(f"ℹ️ CCXT ({client_name}, {market_symbol}) NetworkError/ExchangeError。次のペアを試行。")
            continue
        except Exception:
            continue

    return [], "NoData"

# ... (fetch_order_book_depth_async と generate_signal_candidate は変更なし) ...


# --- 稼働時間維持のための自己Ping関数 (v7.40) ---

async def self_ping_task(interval: int = 55):
    """
    Renderの無料枠での強制シャットダウンを回避するため、
    一定間隔で自身のエンドポイントを叩き、トラフィックを発生させる。
    """
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    
    if not render_url:
        logging.warning("⚠️ RENDER_EXTERNAL_URL環境変数が設定されていません。自己Pingは無効です。")
        return

    logging.info(f"🟢 自己Pingタスクを開始します (インターバル: {interval}秒)。URL: {render_url}")
    
    # RenderのURLが 'https://xxxx.onrender.com' の形式であることを確認
    if not render_url.startswith('https://'):
        render_url = f"https://{render_url}"

    while True:
        await asyncio.sleep(interval)
        try:
            # GET / にアクセス
            response = requests.get(render_url, timeout=5)
            # logging.debug(f"Self-ping successful. Status: {response.status_code}")
            
        except requests.exceptions.RequestException as e:
            logging.debug(f"Self-ping failed: {e}. Retrying.")
        except asyncio.CancelledError:
            break

# --- main_loop (v7.40) ---

async def main_loop():
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS, NOTIFIED_SYMBOLS, NEUTRAL_NOTIFIED_TIME
    global LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    global CCXT_CLIENT_NAME
    
    loop = asyncio.get_event_loop()
    
    # 起動時の初期化
    macro_context_data = await loop.run_in_executor(None, get_tradfi_macro_context)
    CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS 
    LAST_UPDATE_TIME = time.time()
    await send_test_message() 
    
    # 📌 修正: 自己Pingタスクを起動
    asyncio.create_task(self_ping_task(interval=55)) 
    
    current_client_index = 0 
    
    while True:
        try:
            current_time = time.time()
            
            # --- 負荷分散: クライアント切り替え ---
            current_client_name = CCXT_CLIENT_NAMES[current_client_index % len(CCXT_CLIENT_NAMES)]
            CCXT_CLIENT_NAME = current_client_name 
            
            # --- 動的更新フェーズ (5分に一度) ---
            if (current_time - LAST_UPDATE_TIME) >= DYNAMIC_UPDATE_INTERVAL:
                logging.info("==================================================")
                logging.info(f"Apex BOT v7.40 分析サイクル開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
                macro_context_data = await loop.run_in_executor(None, get_tradfi_macro_context)
                logging.info(f"マクロ経済コンテクスト: {macro_context_data['trend']} (VIX: {macro_context_data['vix_level']:.1f}, GVIX: {macro_context_data['gvix_level']:.1f})")
                LAST_UPDATE_TIME = current_time
                logging.info(f"優先データソース: {CCXT_CLIENT_NAME} (非同期並列処理, 遅延: {REQUEST_DELAY}s)")
                logging.info("--------------------------------------------------")
            
            # --- メイン分析実行 (60秒ごと) ---
            candidate_tasks = [generate_signal_candidate(sym, macro_context_data, CCXT_CLIENT_NAME) 
                               for sym in CURRENT_MONITOR_SYMBOLS]
            candidates = await asyncio.gather(*candidate_tasks)
            
            current_client_index += 1

            # 統計情報を更新
            TOTAL_ANALYSIS_ATTEMPTS += len(CURRENT_MONITOR_SYMBOLS)
            success_count = sum(1 for c in candidates if c is not None)
            TOTAL_ANALYSIS_ERRORS += len(CURRENT_MONITOR_SYMBOLS) - success_count
            if success_count > 0:
                LAST_SUCCESS_TIME = current_time

            # 最低スコア閾値 (0.50) で品質の低いシグナルを排除
            valid_candidates = [c for c in candidates if c is not None and c['side'] != "Neutral" and c['score'] >= 0.50]
            neutral_candidates = [c for c in candidates if c is not None and c['side'] == "Neutral"]

            # 3. ロング/ショートの有効候補がある場合 (変更なし)
            if valid_candidates:
                best_signal = max(valid_candidates, key=lambda c: c['score'])
                is_not_recently_notified = current_time - NOTIFIED_SYMBOLS.get(best_signal['symbol'], 0) > 3600

                log_status = "✅ 通知実行" if is_not_recently_notified else "🔒 1時間ロック中"
                logging.info(f"🔔 最優秀候補: {best_signal['symbol']} - {best_signal['side']} (スコア: {best_signal['score']:.4f}) | 状況: {log_status}")

                if is_not_recently_notified:
                    message = format_telegram_message(best_signal)
                    await loop.run_in_executor(None, lambda: send_telegram_html(message, is_emergency=True))
                    NOTIFIED_SYMBOLS[best_signal['symbol']] = current_time
                
            # 4. 中立候補がない、または強制通知が必要な場合 (変更なし)
            time_since_last_neutral = current_time - NEUTRAL_NOTIFIED_TIME
            is_neutral_notify_due = time_since_last_neutral > 1800 
            
            if is_neutral_notify_due:
                logging.info("⚠️ 30分間隔の強制通知時間になりました。")
                
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
                
            
            # 5. ロギング（シグナルも中立通知も行わなかった場合） (変更なし)
            elif not valid_candidates:
                if neutral_candidates:
                     logging.info(f"🔒 30分ロック中 (残り: {max(0, 1800 - time_since_last_neutral):.0f}s)。")

            await asyncio.sleep(LOOP_INTERVAL)
            
        except asyncio.CancelledError:
            logging.warning("バックグラウンドタスクがキャンセルされました。")
            break
        except Exception as e:
            logging.error(f"メインループで予期せぬエラーが発生しました: {type(e).__name__}: {e}。{LOOP_INTERVAL}秒後に再試行します。")
            await asyncio.sleep(LOOP_INTERVAL)


# --- Telegram Message Format (v7.40: 変更なし) ---
def format_telegram_message(signal: Dict) -> str:
    """Telegramメッセージのフォーマット"""
    # ... (前バージョンと同じ) ...
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
    
    if signal['side'] == "Neutral":
        
         if signal.get('is_fallback', False) and signal['symbol'] == "FALLBACK":
             return (
                f"🚨 <b>Apex BOT v7.40 - 死活監視 (システム正常)</b> 🟢\n"
                f"<i>強制通知時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i>\n\n"
                f"• **市場コンテクスト**: {signal['macro_context']['trend']} ({vix_status} | {gvix_status})\n"
                f"• **🤖 BOTヘルス**: 最終成功: {last_success_time} JST\n"
                f"• **データソース**: {CCXT_CLIENT_NAME} が現在メイン (非同期処理)。"
            )
         
         tech_data = signal.get('tech_data', {})
         rsi_str = f"RSI: {tech_data.get('rsi', 50):.1f}"
         macd_hist_str = f"MACD Hist: {tech_data.get('macd_hist', 0):.4f}"

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
            f"• <b>チャート動向</b>: {rsi_str}, {macd_hist_str}\n" 
            f"<b>【BOTの判断】: 現在は待機が最適です。</b>"
        )
    
    score = signal['score']
    side_icon = "⬆️ LONG" if signal['side'] == "ロング" else "⬇️ SHORT"
    source = signal.get('source', 'N/A')
    
    if score >= 0.80:
        score_icon = "🔥🔥🔥"
        lot_size = "MAX"
        action = "積極的なエントリー"
    elif score >= 0.65:
        score_icon = "🔥🌟"
        lot_size = "中〜大"
        action = "標準的なエントリー"
    elif score >= 0.50:
        score_icon = "✨"
        lot_size = "小"
        action = "少額で慎重にエントリー"
    else:
        score_icon = "❌"
        lot_size = "見送り"
        action = "分析エラーまたは品質不足"

    tech_data = signal.get('tech_data', {})
    rsi_str = f"{tech_data.get('rsi', 50):.1f}"
    macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"

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
        f"📈 <b>チャート動向</b>:\n"
        f"  - <i>RSI (モメンタム)</i>: {rsi_str}\n"
        f"  - <i>MACD Hist (トレンド)</i>: {macd_hist_str}\n"
        f"• <i>波形フェーズ</i>: {signal['wave_phase']} | <i>ソース</i>: {source}\n"
        f"• <i>マクロ環境</i>: {vix_status} | {gvix_status}\n"
        f"\n"
        f"💰 <b>取引示唆</b>:\n" 
        f"  - <b>推奨ロット</b>: {lot_size}\n"
        f"  - <b>推奨アクション</b>: {action}\n"
        f"<b>【BOTの判断】: 取引計画に基づきエントリーを検討してください。</b>"
    )

# ------------------------------------------------------------------------------------
# FASTAPI WEB SERVER SETUP (バージョン表記を v7.40 に更新)
# ------------------------------------------------------------------------------------

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    logging.info("Starting Apex BOT Web Service (v7.40 - Free Tier Endurance)...")
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

@app.get("/")
def read_root():
    monitor_info = ", ".join(CURRENT_MONITOR_SYMBOLS[:3]) + "..." if len(CURRENT_MONITOR_SYMBOLS) > 3 else "No Symbols"
    return {
        "status": "Running",
        "service": "Apex BOT v7.40 (Free Tier Endurance)",
        "monitoring_base": CCXT_CLIENT_NAME,
        "monitored_symbols": monitor_info,
        "analysis_interval_s": LOOP_INTERVAL,
        "last_analysis_attempt": datetime.fromtimestamp(LAST_UPDATE_TIME).strftime('%H:%M:%S'),
    }
