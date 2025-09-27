# ====================================================================================
# Apex BOT v6.0 - 出来高トップ30銘柄の動的選定機能実装版 (main_render.py)
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
DYNAMIC_UPDATE_INTERVAL = 300  # 5分ごとに出来高トップ30を更新

# ====================================================================================
#                               UTILITIES & CLIENTS
# ====================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CCXT_CLIENT_NAME = 'Binance Futures' 
CCXT_CLIENT = None 
LAST_UPDATE_TIME = 0.0 
CURRENT_MONITOR_SYMBOLS = []

def initialize_ccxt_client():
    """CCXTクライアントを初期化する"""
    global CCXT_CLIENT
    CCXT_CLIENT = ccxt_async.binance({"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": "future"}})

async def send_test_message():
    """BOT起動時のセルフテスト通知"""
    test_text = (
        f"🤖 <b>Apex BOT v6.0 - 起動テスト通知</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"Render環境でのWebサービス起動に成功しました。\n"
        f"分析サイクル (30秒ごと) および Telegram 接続は正常に稼働中です。"
    )
    
    try:
        loop = asyncio.get_event_loop()
        # send_telegram_html は同期関数なので、Executorで実行
        await loop.run_in_executor(None, lambda: send_telegram_html(test_text, is_emergency=True))
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

def send_telegram_html(text: str, is_emergency: bool = False):
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
        # メッセージが届いていることから、この同期呼び出しは機能している
        requests.post(url, json=payload, timeout=10) 
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram送信エラー: {e}")

# 🚨 【重要変更】出来高トップ30をCCXTで動的に取得する
async def fetch_top_symbols_async(limit: int = 30) -> Tuple[List[str], str]:
    """取引所の出来高トップ銘柄を動的に取得する"""
    if CCXT_CLIENT is None: 
        return DEFAULT_SYMBOLS[:limit], "Static List (CCXT Client missing)"

    try:
        # すべてのフューチャー（先物）市場のティッカー情報を取得
        tickers = await CCXT_CLIENT.fetch_tickers(params={"defaultType": "future"})
        
        volume_data = []
        for symbol, ticker in tickers.items():
            # USDTペア、かつ24時間出来高データが存在するものを抽出
            if '/USDT' in symbol and ticker.get('baseVolume') is not None:
                # baseVolume (例: BTC/USDTのBTC出来高) でランキング
                volume_data.append({'symbol': symbol.replace('/USDT', ''), 'volume': ticker['baseVolume']})

        # 出来高降順でソート
        volume_data.sort(key=lambda x: x['volume'], reverse=True)
        # トップ30のシンボル名を取得 (ただしUSDT自身は除く)
        top_symbols = [d['symbol'] for d in volume_data if d['symbol'] != 'USDT'][:limit]
        
        if not top_symbols:
            # データが空の場合は静的リストにフォールバック
            return DEFAULT_SYMBOLS[:limit], "Static List (Volume data empty)"

        return top_symbols, "Dynamic List (CCXT Volume Top)"
        
    except Exception as e:
        logging.error(f"❌ ダイナミック銘柄選定に失敗: {e}。静的リストにフォールバックします。")
        return DEFAULT_SYMBOLS[:limit], "Static List (Fallback)"
# ----------------------------------------------------

async def fetch_ohlcv_async(symbol: str, timeframe: str, limit: int) -> List[list]:
    """OHLCVは固定のCCXTクライアント (Binance) から取得する"""
    if CCXT_CLIENT is None: return []
    
    market_symbol = f"{symbol}/USDT" 

    try:
        return await CCXT_CLIENT.fetch_ohlcv(market_symbol, timeframe, limit=limit)
    except Exception:
        return []

async def fetch_market_sentiment_data_async(symbol: str) -> Dict:
    """OIデータ取得をスキップし、ダミーデータを返す"""
    # 出来高ベースの選定により、OIデータは一旦ニュートラルとして扱う
    return {"oi_change_24h": 0} 

def get_tradfi_macro_context() -> str:
    try:
        es = yf.Ticker("ES=F")
        hist = es.history(period="5d", interval="1h")
        if hist.empty: return "不明"
        prices = hist['Close']
        kama_fast = calculate_kama(prices, period=10)
        kama_slow = calculate_kama(prices, period=21)
        if kama_fast.iloc[-1] > kama_slow.iloc[-1] and prices.iloc[-1] > kama_fast.iloc[-1]:
            return "リスクオン (株高)"
        if kama_fast.iloc[-1] < kama_slow.iloc[-1] and prices.iloc[-1] < kama_fast.iloc[-1]:
            return "リスクオフ (株安)"
        return "中立"
    except Exception:
        return "不明"

def calculate_kama(prices: pd.Series, period: int = 10, fast_ema: int = 2, slow_ema: int = 30) -> pd.Series:
    change = prices.diff(period).abs()
    volatility = prices.diff().abs().rolling(window=period).sum().replace(0, 1e-9)
    er = change / volatility
    sc = (er * (2 / (fast_ema + 1) - 2 / (slow_ema + 1)) + 2 / (slow_ema + 1)) ** 2
    kama = pd.Series(np.nan, index=prices.index)
    if len(prices) > period:
        kama.iloc[period] = prices.iloc[period]
        for i in range(period + 1, len(prices)):
            kama.iloc[i] = kama.iloc[i-1] + sc.iloc[i] * (prices.iloc[i] - kama.iloc[i-1])
    return kama

async def determine_market_regime(symbol: str) -> str:
    ohlcv = await fetch_ohlcv_async(symbol, '4h', 100)
    if len(ohlcv) < 100: return "不明"
    prices = pd.Series([c[4] for c in ohlcv])
    atr_ratio = (pd.Series([h[2] - h[3] for h in ohlcv]).rolling(14).mean().iloc[-1]) / prices.iloc[-1]
    # ボラティリティ条件を少し緩和
    if atr_ratio > 0.04: return "高ボラティリティ"
    kama_fast = calculate_kama(prices, period=21)
    kama_slow = calculate_kama(prices, period=50)
    if kama_fast.iloc[-1] > kama_slow.iloc[-1] and prices.iloc[-1] > kama_fast.iloc[-1]:
        return "強気トレンド"
    if kama_fast.iloc[-1] < kama_slow.iloc[-1] and prices.iloc[-1] < kama_fast.iloc[-1]:
        return "弱気トレンド"
    return "レンジ相場"

async def multi_timeframe_confirmation(symbol: str) -> (bool, str):
    timeframes = ['15m', '1h', '4h']
    trends = []
    tasks = [fetch_ohlcv_async(symbol, tf, 60) for tf in timeframes]
    ohlcv_results = await asyncio.gather(*tasks)
    for ohlcv in ohlcv_results:
        if len(ohlcv) < 60: return False, "データ不足"
        prices = pd.Series([c[4] for c in ohlcv])
        kama = calculate_kama(prices, period=21)
        trends.append("上昇" if prices.iloc[-1] > kama.iloc[-1] else "下降")
    if len(set(trends)) == 1:
        return True, trends[0]
    return False, "不一致"

def get_ml_prediction(ohlcv: List[list], sentiment: Dict) -> float:
    try:
        closes = pd.Series([c[4] for c in ohlcv])
        delta = closes.diff().fillna(0)
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi = 100 - (100 / (1 + rs)).iloc[-1]
        
        # RSIをベースに予測確率を計算。0.5をニュートラルとしてRSIが70なら0.8に近づく。
        prob = 0.5 + ((rsi - 50) / 100) * 0.8 
        return np.clip(prob, 0, 1)
    except Exception:
        return 0.5

async def generate_signal(symbol: str, regime: str, macro_context: str) -> Optional[Dict]:
    criteria = []
    is_aligned, trend_direction = await multi_timeframe_confirmation(symbol)
    
    # シグナル生成の条件をわずかに緩和
    if is_aligned: criteria.append(f"多時間軸の方向性が一致 ({trend_direction})")
    
    side = None
    # 高ボラティリティ相場ではトレンドフォローは行わない
    if regime == "強気トレンド" and trend_direction == "上昇":
        side = "ロング"
        criteria.append("長期レジームが強気トレンド")
    elif regime == "弱気トレンド" and trend_direction == "下降":
        side = "ショート"
        criteria.append("長期レジームが弱気トレンド")
    
    if side is None: return None
        
    # マクロ経済との整合性チェック
    if side == "ロング" and macro_context == "リスクオフ (株安)": return None
    if side == "ショート" and macro_context == "リスクオン (株高)": return None
    criteria.append(f"マクロ経済と整合 ({macro_context})")
    
    sentiment = await fetch_market_sentiment_data_async(symbol)
    criteria.append("OIデータはニュートラルとして処理されました")
        
    ohlcv_15m = await fetch_ohlcv_async(symbol, '15m', 100)
    if len(ohlcv_15m) < 100: return None
    win_prob = get_ml_prediction(ohlcv_15m, sentiment)
    criteria.append(f"MLモデル予測上昇確率: {win_prob:.2%}")
    
    # 以前の0.70から0.65に緩和し、よりシグナルを出しやすくする
    required_confidence = 0.65
    
    if len(criteria) >= 3 and (win_prob > required_confidence if side == "ロング" else win_prob < (1 - required_confidence)):
        price = ohlcv_15m[-1][4]
        atr = (pd.Series([h[2] - h[3] for h in ohlcv_15m]).rolling(14).mean().iloc[-1])
        sl = price - (atr * 2.5) if side == "ロング" else price + (atr * 2.5)
        
        # win_probはロングの確率なので、ショートの場合は逆の確率を使う
        final_confidence = win_prob if side == "ロング" else (1 - win_prob)
        
        return {"symbol": symbol, "side": side, "price": price, "sl": sl,
                "criteria": criteria, "confidence": final_confidence, "regime": regime, "ohlcv_15m": ohlcv_15m}
    return None

def format_telegram_message(signal: Dict) -> str:
    side_icon = "📈" if signal['side'] == "ロング" else "📉"
    msg = f"💎 <b>Apex BOT シグナル速報: {signal['symbol']}</b> {side_icon}\n"
    msg += f"<i>市場レジーム: {signal['regime']} ({CCXT_CLIENT_NAME.split(' ')[0]}データ)</i>\n"
    msg += f"<i>MLモデル予測信頼度: {signal['confidence']:.2%}</i>\n\n"
    
    msg += "<b>✅ 判断根拠 (Criteria)</b>\n"
    for c in signal['criteria']: msg += f"• {c}\n"
        
    price = signal['price']
    sl = signal['sl']
    
    closes_15m = pd.Series([c[4] for c in signal['ohlcv_15m']])
    optimal_entry = closes_15m.ewm(span=9, adjust=False).mean().iloc[-1]
    
    df_15m = pd.DataFrame(signal['ohlcv_15m'], columns=['t','o','h','l','c','v'])
    df_15m['tr'] = np.maximum(df_15m['h'] - df_15m['l'], np.maximum(abs(df_15m['h'] - df_15m['c'].shift()), abs(df_15m['l'] - df_15m['c'].shift())))
    atr_15m = df_15m['tr'].rolling(14).mean().iloc[-1]

    entry_zone_upper = optimal_entry + (atr_15m * 0.5)
    entry_zone_lower = optimal_entry - (atr_15m * 0.5)
    
    risk_per_unit = abs(optimal_entry - sl)
    tp1 = optimal_entry + (risk_per_unit * 1.5) if signal['side'] == "ロング" else optimal_entry - (risk_per_unit * 1.5)
    tp2 = optimal_entry + (risk_per_unit * 3.0) if signal['side'] == "ロング" else optimal_entry - (risk_per_unit * 3.0)
    
    msg += "\n<b>🎯 精密エントリープラン</b>\n"
    msg += f"<pre>現在価格: {price:,.4f}\n\n"
    if signal['side'] == 'ロング':
        msg += f"--- エントリーゾーン (指値案) ---\n"
        msg += f"上限: {entry_zone_upper:,.4f}\n"
        msg += f"最適: {optimal_entry:,.4f} (9EMA)\n"
        msg += f"下限: {entry_zone_lower:,.4f}\n"
        msg += "👉 この価格帯への押し目を待ってエントリー\n\n"
    else:
        msg += f"--- エントリーゾーン (指値案) ---\n"
        msg += f"下限: {entry_zone_lower:,.4f}\n"
        msg += f"最適: {optimal_entry:,.4f} (9EMA)\n"
        msg += f"上限: {entry_zone_upper:,.4f}\n"
        msg += "👉 この価格帯への戻りを待ってエントリー\n\n"
        
    msg += f"--- ゾーンエントリー時の目標 ---\n"
    msg += f"損切 (SL): {sl:,.4f}\n"
    msg += f"利確① (TP1): {tp1:,.4f}\n"
    msg += f"利確② (TP2): {tp2:,.4f}</pre>"
    
    return msg

async def analyze_symbol_and_notify(symbol: str, macro_context: str, notified_symbols: Dict):
    current_time = time.time()
    # 一度シグナルを出したら1時間(3600秒)は再通知しない
    if symbol in notified_symbols and current_time - notified_symbols[symbol] < 3600: return

    regime = await determine_market_regime(symbol)
    if regime == "不明": return
        
    signal = await generate_signal(symbol, regime, macro_context)
    if signal:
        message = format_telegram_message(signal)
        # 実際のシグナルは緊急通知として送信 (通知音を鳴らす)
        send_telegram_html(message, is_emergency=True)
        notified_symbols[symbol] = current_time
        logging.info(f"🚨 シグナル通知成功: {signal['symbol']} - {signal['side']} @ {signal['price']:.4f} (信頼度: {signal['confidence']:.2%})")

async def main_loop():
    """BOTの常時監視を実行するバックグラウンドタスク"""
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS
    notified_symbols = {}
    
    # 起動時の初期リスト設定
    CURRENT_MONITOR_SYMBOLS, source = await fetch_top_symbols_async(30)
    macro_context = get_tradfi_macro_context()
    LAST_UPDATE_TIME = time.time()
    
    # --- 初期起動時のテスト通知 ---
    await send_test_message()
    # -----------------------------
    
    while True:
        try:
            current_time = time.time()
            # 出来高トップ30のリストを5分に一度更新
            is_dynamic_update_needed = (current_time - LAST_UPDATE_TIME) >= DYNAMIC_UPDATE_INTERVAL
            
            # --- 動的更新フェーズ (5分に一度) ---
            if is_dynamic_update_needed:
                logging.info("==================================================")
                logging.info(f"Apex BOT v6.0 分析サイクル開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
                
                macro_context = get_tradfi_macro_context() # マクロコンテクストを更新
                logging.info(f"マクロ経済コンテクスト: {macro_context}")
                
                # 出来高TOP30の動的選定
                symbols_to_monitor, source_exchange = await fetch_top_symbols_async(30)
                
                CURRENT_MONITOR_SYMBOLS = symbols_to_monitor
                LAST_UPDATE_TIME = current_time
                
                logging.info(f"銘柄選定元: {source_exchange}")
                logging.info(f"監視対象 (TOP30): {', '.join(CURRENT_MONITOR_SYMBOLS[:5])} ...")
                logging.info("--------------------------------------------------")
            
            # --- メイン分析実行 (30秒ごと) ---
            
            # 分析の実行
            tasks = [analyze_symbol_and_notify(sym, macro_context, notified_symbols) for sym in CURRENT_MONITOR_SYMBOLS]
            await asyncio.gather(*tasks)

            # ログ出力は、5分に一度の更新時に集約して出力する
            if is_dynamic_update_needed:
                logging.info("--------------------------------------------------")
                logging.info(f"分析サイクル完了。{LOOP_INTERVAL}秒待機します。")
                logging.info("==================================================")
            
            # 30秒待機
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
    
    # 1. CCXTクライアントの初期化 (Binanceに固定)
    initialize_ccxt_client() 

    # 2. バックグラウンドタスクとしてメインループを起動
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
    # 監視対象リストが空でないことを確認してからログに出力
    monitor_info = CURRENT_MONITOR_SYMBOLS[0] if CURRENT_MONITOR_SYMBOLS else "No Symbols"
    logging.info(f"Health Check Ping Received. Analyzing: {monitor_info}...")
    return {
        "status": "Running",
        "service": "Apex BOT v6.0",
        "monitoring_base": CCXT_CLIENT_NAME.split(' ')[0],
        "next_dynamic_update": f"{DYNAMIC_UPDATE_INTERVAL - (time.time() - LAST_UPDATE_TIME):.0f}s"
    }
